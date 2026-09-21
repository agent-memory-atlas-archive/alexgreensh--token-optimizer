import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compaction_timing_eval.py"
FIXTURE = ROOT / "tests" / "fixtures" / "compaction-timing" / "challenge-v1.json"

spec = importlib.util.spec_from_file_location("compaction_timing_eval", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)


def corpus():
    return json.loads(FIXTURE.read_text())


def test_committed_corpus_covers_required_scenarios_hosts_and_bands():
    data = mod.load_corpus(FIXTURE)
    assert {s["scenario"] for s in data["sessions"]} == mod.SCENARIOS
    assert {s["host"] for s in data["sessions"]} >= {"claude", "codex", "opencode", "hermes"}
    bands = {mod.occupancy_band(cp["occupancy_pct"]) for s in data["sessions"] for cp in s["checkpoints"]}
    assert bands == {"<50", "50-69", "70-79", "80+"}


def test_current_semantic_and_hybrid_decisions_are_distinct():
    cp = corpus()["sessions"][0]["checkpoints"][3]
    cp.update(occupancy_pct=90, quality_score=40, settled=False, completion_cue=False, pending_work=True)
    assert mod.policy_decision("current_advisory", cp) is True
    assert mod.policy_decision("semantic_boundary", cp) is False
    assert mod.policy_decision("hybrid", cp) is False


def test_observed_policy_override_wins_over_reconstruction():
    cp = corpus()["sessions"][0]["checkpoints"][0]
    cp["policy_observed"] = {"current_advisory": True}
    assert mod.policy_decision("current_advisory", cp) is True


def test_unknown_product_truth_is_excluded_and_counted():
    data = corpus()
    for s in data["sessions"]:
        for cp in s["checkpoints"]:
            cp["policy_observed"] = {"current_advisory": False}
    cp = data["sessions"][0]["checkpoints"][0]
    cp["policy_observed"]["current_advisory"] = True
    cp["safe_boundary"] = True
    cp["next_turn_needed_older_context"] = None
    metric = mod.evaluate(data)["policies"]["current_advisory"]
    assert metric["product_truth_precision"]["denominator"] == 0
    assert metric["product_truth_unknown_recommendations"] == 1


def test_split_is_deterministic_and_session_grouped():
    data = corpus()
    train_a, eval_a = mod.split_corpus(data, .7, 17)
    train_b, eval_b = mod.split_corpus(data, .7, 17)
    assert train_a == train_b and eval_a == eval_b
    mod.assert_disjoint(train_a, eval_a)
    assert len(train_a["sessions"]) + len(eval_a["sessions"]) == len(data["sessions"])
    assert {s["scenario"] for s in train_a["sessions"]} == mod.SCENARIOS
    assert {s["scenario"] for s in eval_a["sessions"]} == mod.SCENARIOS


def test_overlap_is_a_hard_error_even_when_checkpoint_ids_differ():
    data = corpus()
    train = {"schema_version": 1, "corpus": {}, "sessions": [copy.deepcopy(data["sessions"][0])]}
    evaluation = {"schema_version": 1, "corpus": {}, "sessions": [copy.deepcopy(data["sessions"][0])]}
    evaluation["sessions"][0]["checkpoints"][0]["checkpoint_id"] = "different"
    with pytest.raises(ValueError, match="session leakage"):
        mod.assert_disjoint(train, evaluation)

@pytest.mark.parametrize("field,value", [
    ("occupancy_pct", 101), ("quality_score", float("nan")),
    ("pending_work", "false"), ("next_turn_needed_older_context", "unknown"),
])
def test_schema_rejects_malformed_checkpoint_fields(field, value):
    data = corpus(); data["sessions"][0]["checkpoints"][0][field] = value
    with pytest.raises(ValueError):
        mod.validate_corpus(data)



def test_schema_rejects_raw_text_and_unknown_fields():
    data = corpus(); data["sessions"][0]["checkpoints"][0]["message"] = "private text"
    with pytest.raises(ValueError, match="unknown fields"):
        mod.validate_corpus(data)



def test_empty_and_non_object_corpora_are_rejected():
    with pytest.raises(ValueError, match="root must be an object"):
        mod.validate_corpus([])
    with pytest.raises(ValueError, match="at least one session"):
        mod.validate_corpus({"schema_version": 1, "corpus": {}, "sessions": []})


def test_metadata_rejects_free_text_in_every_possible_field():
    secret = "USER: my secret raw prompt content"
    for field in ("name", "description", "limitations", "labeling"):
        data = corpus(); data["corpus"][field] = secret
        with pytest.raises(ValueError, match="unknown corpus metadata"):
            mod.validate_corpus(data)
    data = corpus(); data["corpus"]["corpus_id"] = secret
    with pytest.raises(ValueError, match="anonymous ID characters"):
        mod.validate_corpus(data)


def test_identifiers_and_host_are_bounded_anonymous_strings():
    for field, value in (("session_id", "has spaces"), ("source_group_id", ["bad"]), ("host", ["bad"])):
        data = corpus(); data["sessions"][0][field] = value
        with pytest.raises(ValueError):
            mod.validate_corpus(data)


def test_boolean_is_not_a_number():
    data = corpus(); data["sessions"][0]["checkpoints"][0]["occupancy_pct"] = True
    with pytest.raises(ValueError, match="occupancy_pct"):
        mod.validate_corpus(data)


def test_group_and_renamed_duplicate_leakage_are_rejected():
    data = corpus(); left = copy.deepcopy(data["sessions"][0]); right = copy.deepcopy(left)
    right["session_id"] = "renamed-session"; right["source_group_id"] = "renamed-group"
    train = {"schema_version": 1, "corpus": {}, "sessions": [left]}
    evaluation = {"schema_version": 1, "corpus": {}, "sessions": [right]}
    with pytest.raises(ValueError, match="content signature leakage"):
        mod.assert_disjoint(train, evaluation)
    right["checkpoints"][0]["quality_score"] -= 1; right["source_group_id"] = left["source_group_id"]
    with pytest.raises(ValueError, match="source group leakage"):
        mod.assert_disjoint(train, evaluation)


def test_randomized_splits_keep_groups_and_signatures_disjoint():
    data = corpus()
    # Deliberately add renamed byte-equivalent sessions under the same source group.
    for original in list(data["sessions"][:5]):
        duplicate = copy.deepcopy(original); duplicate["session_id"] += "-duplicate"
        data["sessions"].append(duplicate)
    for seed in range(30):
        train, evaluation = mod.split_corpus(data, .7, seed)
        mod.assert_disjoint(train, evaluation)


def test_cli_malformed_inputs_exit_two_without_traceback(tmp_path):
    for value in ([], {"schema_version": 1, "corpus": {}, "sessions": []},
                  {"schema_version": 1, "corpus": {}, "sessions": [{"host": []}]}):
        path = tmp_path / "bad.json"; path.write_text(json.dumps(value))
        run = subprocess.run([sys.executable, str(SCRIPT), "run", "--corpus", str(path)], text=True, capture_output=True)
        assert run.returncode == 2
        assert "Traceback" not in run.stderr


def test_split_outputs_only_safe_metadata():
    data = corpus(); data["corpus"] = {"corpus_id": "private-corpus"}
    train, evaluation = mod.split_corpus(data, .7, 4)
    assert train["corpus"] == {"corpus_id": "private-corpus", "split_role": "train", "split_seed": 4}
    assert evaluation["corpus"] == {"corpus_id": "private-corpus", "split_role": "evaluation", "split_seed": 4}


def test_hybrid_honors_observed_current_decision_truth_table():
    cp = corpus()["sessions"][0]["checkpoints"][0]
    cp.update(settled=True, completion_cue=True, pending_work=False, occupancy_pct=95, quality_score=40,
              policy_observed={"current_advisory": False})
    assert mod.policy_decision("current_advisory", cp) is False
    assert mod.policy_decision("hybrid", cp) is False
    cp.update(occupancy_pct=10, quality_score=99, policy_observed={"current_advisory": True})
    assert mod.policy_decision("current_advisory", cp) is True
    assert mod.policy_decision("hybrid", cp) is True


def test_bootstrap_clusters_whole_source_groups(monkeypatch):
    data = corpus(); first = data["sessions"][0]
    duplicate = copy.deepcopy(first); duplicate["session_id"] += "-retry"
    rows = [{"session": first, "checkpoint": first["checkpoints"][0]},
            {"session": duplicate, "checkpoint": duplicate["checkpoints"][0]}]
    choices = []
    class SpyRandom:
        def __init__(self, *_): pass
        def choice(self, values): choices.append(tuple(values)); return values[0]
    monkeypatch.setattr(mod.random, "Random", SpyRandom)
    mod._cluster_bootstrap(rows, "current_advisory", "precision", samples=1)
    assert choices == [(first["source_group_id"],)]


def test_source_group_cannot_span_scenarios():
    data = corpus(); data["sessions"][1]["source_group_id"] = data["sessions"][0]["source_group_id"]
    data["sessions"][1]["scenario"] = "coordination"
    with pytest.raises(ValueError, match="spans multiple scenarios"):
        mod.validate_corpus(data)


def test_real_rows_require_observed_current_advisory():
    data = corpus(); data["sessions"][0]["provenance"] = "sanitized_real"
    with pytest.raises(ValueError, match="requires policy_observed.current_advisory"):
        mod.validate_corpus(data)
    for cp in data["sessions"][0]["checkpoints"]:
        cp["policy_observed"] = {"current_advisory": False}
    mod.validate_corpus(data)

def test_cli_rejects_session_leakage(tmp_path):
    data = corpus(); train = tmp_path / "train.json"; evaluation = tmp_path / "eval.json"
    train.write_text(json.dumps(data)); evaluation.write_text(json.dumps(data))
    run = subprocess.run([sys.executable, str(SCRIPT), "run", "--corpus", str(evaluation), "--train-corpus", str(train)], text=True, capture_output=True)
    assert run.returncode == 2
    assert "session leakage" in run.stderr


def test_cli_split_then_run(tmp_path):
    train = tmp_path / "train.json"; evaluation = tmp_path / "eval.json"
    split = subprocess.run([sys.executable, str(SCRIPT), "split", "--corpus", str(FIXTURE), "--train-output", str(train), "--eval-output", str(evaluation), "--seed", "9"], capture_output=True, text=True)
    assert split.returncode == 0, split.stderr
    run = subprocess.run([sys.executable, str(SCRIPT), "run", "--corpus", str(evaluation), "--train-corpus", str(train), "--json"], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report["sessions"] > 0
    assert set(report["by_occupancy_band"]) == {"<50", "50-69", "70-79", "80+"}


def test_continuity_is_not_counted_as_advisory_recommendation():
    report = mod.evaluate(corpus())
    assert report["continuity_protection"]["risky_checkpoints"] > 0
    assert report["policies"]["current_advisory"]["recommendations"] != report["continuity_protection"]["recent_checkpoint_coverage"]["numerator"]
