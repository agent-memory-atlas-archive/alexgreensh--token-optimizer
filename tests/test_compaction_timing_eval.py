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
    train = {"schema_version": 1, "sessions": [copy.deepcopy(data["sessions"][0])]}
    evaluation = {"schema_version": 1, "sessions": [copy.deepcopy(data["sessions"][0])]}
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
