#!/usr/bin/env python3
"""Evaluate compaction/restart timing without changing runtime behavior.

The evaluator keeps task-boundary safety, future context need, and continuity
protection separate. Input corpora are session-grouped JSON; checkpoints from a
session are never split across train/evaluation partitions.
"""
from __future__ import annotations

import argparse
import re
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCENARIOS = {"mid_task_coding", "coordination", "blocked_work", "agent_fanout", "long_supervision"}
ROOT_KEYS = {"schema_version", "corpus", "sessions"}
METADATA_KEYS = {"corpus_id", "split_role", "split_seed"}
SESSION_KEYS = {"session_id", "source_group_id", "host", "scenario", "provenance", "checkpoints"}
CHECKPOINT_KEYS = {"checkpoint_id", "occupancy_pct", "quality_score", "compaction_depth", "settled", "completion_cue", "pending_work", "checkpoint_age_seconds", "cold_resume_available", "safe_boundary", "next_turn_needed_older_context", "policy_observed"}
PROVENANCE = {"real", "sanitized_real", "synthetic"}
POLICIES = ("current_advisory", "semantic_boundary", "hybrid")
ID_PATTERNS = {
    "corpus": re.compile(r"^corpus-[0-9a-f]{16,64}$"),
    "session": re.compile(r"^session-[0-9a-f]{16,64}$"),
    "group": re.compile(r"^group-[0-9a-f]{16,64}$"),
    "checkpoint": re.compile(r"^checkpoint-[0-9]{1,12}$"),
}
HOSTS = {"claude", "codex", "opencode", "openclaw", "hermes", "copilot", "cursor", "antigravity", "cowork", "grok", "pi"}
MAX_INPUT_BYTES = 10_000_000
MAX_SESSIONS = 10_000
MAX_CHECKPOINTS = 100_000
MAX_METADATA_LENGTH = 2_000


def occupancy_band(value: float) -> str:
    if value < 50:
        return "<50"
    if value < 70:
        return "50-69"
    if value < 80:
        return "70-79"
    return "80+"


def _bounded_string(value, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_METADATA_LENGTH:
        raise ValueError(f"{label} must be a non-empty bounded string")
    return value


def _typed_id(value, label: str, kind: str) -> str:
    if not isinstance(value, str) or not ID_PATTERNS[kind].fullmatch(value):
        raise ValueError(f"{label} must be an opaque {kind}-typed identifier")
    return value

def _metadata(value, source: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{source}: corpus metadata must be an object")
    unknown = set(value) - METADATA_KEYS
    if unknown:
        raise ValueError(f"{source}: unknown corpus metadata fields: {', '.join(sorted(unknown))}")
    clean = {}
    for key, item in value.items():
        if key == "split_seed":
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{source}: split_seed must be an integer")
            clean[key] = item
        elif key == "split_role":
            if item not in {"train", "evaluation"}:
                raise ValueError(f"{source}: split_role must be train or evaluation")
            clean[key] = item
        else:
            clean[key] = _typed_id(item, f"{source}: corpus.{key}", "corpus")
    return clean


def load_corpus(path: str | Path) -> dict:
    source = Path(path)
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError(f"{path}: input exceeds {MAX_INPUT_BYTES} bytes")
    with source.open(encoding="utf-8") as handle:
        data = json.load(handle)
    validate_corpus(data, str(path))
    data["corpus"] = _metadata(data.get("corpus"), str(path))
    return data


def validate_corpus(data: dict, source: str = "corpus") -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{source}: root must be an object")
    if data.get("schema_version") != 1 or not isinstance(data.get("sessions"), list):
        raise ValueError(f"{source}: expected schema_version 1 and sessions[]")
    unknown_root = set(data) - ROOT_KEYS
    if unknown_root:
        raise ValueError(f"{source}: unknown root fields: {', '.join(sorted(unknown_root))}")
    _metadata(data.get("corpus"), source)
    if not data["sessions"]:
        raise ValueError(f"{source}: at least one session is required")
    if len(data["sessions"]) > MAX_SESSIONS:
        raise ValueError(f"{source}: too many sessions (max {MAX_SESSIONS})")
    seen: set[str] = set(); checkpoints_total = 0; group_scenario: dict[str, str] = {}
    for s_idx, session in enumerate(data["sessions"]):
        if not isinstance(session, dict):
            raise ValueError(f"{source}: session {s_idx} must be an object")
        unknown_session = set(session) - SESSION_KEYS
        if unknown_session:
            raise ValueError(f"{source}: session {s_idx} has unknown fields: {', '.join(sorted(unknown_session))}")
        sid = _typed_id(session.get("session_id"), f"{source}: session {s_idx} session_id", "session")
        group_id = _typed_id(session.get("source_group_id"), f"{source}: {sid} source_group_id", "group")
        _bounded_string(session.get("host"), f"{source}: {sid} host")
        if session["host"] not in HOSTS:
            raise ValueError(f"{source}: {sid} has unsupported host")
        if sid in seen:
            raise ValueError(f"{source}: session {s_idx} has duplicate session_id")
        seen.add(sid)
        if session.get("scenario") not in SCENARIOS:
            raise ValueError(f"{source}: {sid} has unknown scenario")
        prior_scenario = group_scenario.setdefault(group_id, session["scenario"])
        if prior_scenario != session["scenario"]:
            raise ValueError(f"{source}: source_group_id {group_id} spans multiple scenarios")
        if session.get("provenance") not in PROVENANCE:
            raise ValueError(f"{source}: {sid} has unknown provenance")
        checkpoints = session.get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            raise ValueError(f"{source}: {sid} needs at least one checkpoint")
        checkpoints_total += len(checkpoints)
        if checkpoints_total > MAX_CHECKPOINTS:
            raise ValueError(f"{source}: too many checkpoints (max {MAX_CHECKPOINTS})")
        cp_seen: set[str] = set()
        for c_idx, cp in enumerate(checkpoints):
            if not isinstance(cp, dict):
                raise ValueError(f"{source}: {sid} checkpoint {c_idx} must be an object")
            unknown_checkpoint = set(cp) - CHECKPOINT_KEYS
            if unknown_checkpoint:
                raise ValueError(f"{source}: {sid} checkpoint {c_idx} has unknown fields: {', '.join(sorted(unknown_checkpoint))}")
            cid = _typed_id(cp.get("checkpoint_id"), f"{source}: {sid} checkpoint_id", "checkpoint")
            if cid in cp_seen:
                raise ValueError(f"{source}: {sid} duplicate checkpoint_id")
            cp_seen.add(cid)
            for key in ("occupancy_pct", "quality_score", "compaction_depth"):
                if isinstance(cp.get(key), bool) or not isinstance(cp.get(key), (int, float)) or not math.isfinite(cp[key]):
                    raise ValueError(f"{source}: {sid}/{cid} invalid {key}")
            if not 0 <= cp["occupancy_pct"] <= 100 or not 0 <= cp["quality_score"] <= 100:
                raise ValueError(f"{source}: {sid}/{cid} percentage/score out of range")
            if cp["compaction_depth"] < 0:
                raise ValueError(f"{source}: {sid}/{cid} compaction_depth must be nonnegative")
            for key in ("settled", "completion_cue", "pending_work", "cold_resume_available", "safe_boundary"):
                if not isinstance(cp.get(key), bool):
                    raise ValueError(f"{source}: {sid}/{cid} invalid {key}")
            if cp.get("next_turn_needed_older_context") not in (True, False, None):
                raise ValueError(f"{source}: {sid}/{cid} invalid product-truth label")
            age = cp.get("checkpoint_age_seconds")
            if age is not None and (isinstance(age, bool) or not isinstance(age, (int, float)) or age < 0 or not math.isfinite(age)):
                raise ValueError(f"{source}: {sid}/{cid} invalid checkpoint age")
            overrides = cp.get("policy_observed", {})
            if not isinstance(overrides, dict) or any(k != "current_advisory" or not isinstance(v, bool) for k, v in overrides.items()):
                raise ValueError(f"{source}: {sid}/{cid} invalid policy_observed")
            if session["provenance"] != "synthetic" and "current_advisory" not in overrides:
                raise ValueError(f"{source}: {sid}/{cid} real-session replay requires policy_observed.current_advisory")


def _signature(session: dict) -> str:
    clean = [{k: v for k, v in cp.items() if k not in {"checkpoint_id", "policy_observed"}} for cp in session["checkpoints"]]
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assert_disjoint(train: dict, evaluation: dict) -> None:
    for label, key_fn in (("session", lambda s: s["session_id"]), ("source group", lambda s: s["source_group_id"]), ("content signature", _signature)):
        left = {key_fn(s) for s in train["sessions"]}; right = {key_fn(s) for s in evaluation["sessions"]}
        overlap = sorted(left & right)
        if overlap:
            raise ValueError(f"{label} leakage between train and evaluation: " + ", ".join(overlap[:10]))

def policy_decision(name: str, cp: dict) -> bool:
    observed = cp.get("policy_observed", {})
    if name == "current_advisory" and name in observed:
        return observed[name]
    risk = cp["occupancy_pct"] >= 90 or (cp["occupancy_pct"] >= 45 and cp["quality_score"] < 70)
    semantic = cp["settled"] and cp["completion_cue"] and not cp["pending_work"]
    if name == "current_advisory":
        return risk
    if name == "semantic_boundary":
        return semantic
    if name == "hybrid":
        return policy_decision("current_advisory", cp) and semantic
    raise ValueError(f"unknown policy {name}")


def _rate(successes: int, total: int) -> dict:
    return {"value": round(successes / total, 4) if total else None, "numerator": successes,
            "denominator": total}


def _cluster_bootstrap(rows: list[dict], name: str, metric: str, samples: int = 2000) -> list[float] | None:
    """Percentile interval from whole-source-group resampling, never checkpoint resampling."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["session"]["source_group_id"]].append(row)
    ids = sorted(grouped)
    if not ids:
        return None
    rng = random.Random(f"compaction-timing-v1:{name}:{metric}")
    values = []
    for _ in range(samples):
        sampled = [row for _sid in range(len(ids)) for row in grouped[rng.choice(ids)]]
        decisions = [(policy_decision(name, r["checkpoint"]), r["checkpoint"]) for r in sampled]
        if metric == "precision":
            den = sum(d for d, _ in decisions); num = sum(d and cp["safe_boundary"] for d, cp in decisions)
        elif metric == "recall":
            den = sum(cp["safe_boundary"] for _, cp in decisions); num = sum(d and cp["safe_boundary"] for d, cp in decisions)
        else:
            known = [(d, cp) for d, cp in decisions if d and cp["next_turn_needed_older_context"] is not None]
            den = len(known); num = sum(not cp["next_turn_needed_older_context"] for _, cp in known)
        if den:
            values.append(num / den)
    if not values:
        return None
    values.sort()
    return [round(values[int(.025 * (len(values) - 1))], 4), round(values[int(.975 * (len(values) - 1))], 4)]


def _policy_metrics(rows: list[dict], name: str, intervals: bool = False) -> dict:
    tp = fp = tn = fn = 0
    product_good = product_known = unknown = recommendations = 0
    for row in rows:
        decision = policy_decision(name, row["checkpoint"])
        truth = row["checkpoint"]["safe_boundary"]
        recommendations += int(decision)
        if decision and truth: tp += 1
        elif decision and not truth: fp += 1
        elif not decision and truth: fn += 1
        else: tn += 1
        if decision:
            future = row["checkpoint"]["next_turn_needed_older_context"]
            if future is None:
                unknown += 1
            else:
                product_known += 1
                product_good += int(not future)
    precision = _rate(tp, tp + fp)
    recall = _rate(tp, tp + fn)
    if intervals:
        precision["session_cluster_bootstrap_95"] = _cluster_bootstrap(rows, name, "precision")
        recall["session_cluster_bootstrap_95"] = _cluster_bootstrap(rows, name, "recall")
    p, r = precision["value"], recall["value"]
    return {
        "checkpoints": len(rows), "recommendations": recommendations,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "safe_boundary_precision": precision, "safe_boundary_recall": recall,
        "safe_boundary_f1": round(2 * p * r / (p + r), 4) if p is not None and r is not None and p + r else None,
        "product_truth_precision": {**_rate(product_good, product_known), **({"session_cluster_bootstrap_95": _cluster_bootstrap(rows, name, "product")} if intervals else {})},
        "product_truth_unknown_recommendations": unknown,
    }


def evaluate(corpus: dict, train: dict | None = None) -> dict:
    if train is not None:
        assert_disjoint(train, corpus)
    rows = [{"session": s, "checkpoint": cp} for s in corpus["sessions"] for cp in s["checkpoints"]]
    report = {
        "schema_version": 1,
        "corpus": {key: corpus.get("corpus", {}).get(key) for key in ("split_role", "split_seed") if key in corpus.get("corpus", {})},
        "corpus_label": "synthetic challenge corpus" if all(s["provenance"] == "synthetic" for s in corpus["sessions"]) else "private real-session corpus",
        "sessions": len(corpus["sessions"]), "checkpoints": len(rows),
        "checkpoints_by_provenance": dict(sorted(Counter(r["session"]["provenance"] for r in rows).items())),
        "policies": {name: _policy_metrics(rows, name, intervals=True) for name in POLICIES},
        "by_occupancy_band": {}, "by_scenario": {}, "by_host": {},
    }
    dimensions = {
        "by_occupancy_band": (("<50", "50-69", "70-79", "80+"), lambda r: occupancy_band(r["checkpoint"]["occupancy_pct"])),
        "by_scenario": ((), lambda r: r["session"]["scenario"]),
        "by_host": ((), lambda r: r["session"].get("host", "unknown")),
    }
    for out_key, (order, key_fn) in dimensions.items():
        grouped: dict[str, list] = defaultdict(list)
        for row in rows:
            grouped[key_fn(row)].append(row)
        keys = list(order) + sorted(set(grouped) - set(order))
        report[out_key] = {k: {name: _policy_metrics(grouped[k], name) for name in POLICIES} for k in keys}

    risky = [r for r in rows if r["checkpoint"]["occupancy_pct"] >= 50 or r["checkpoint"]["quality_score"] < 70]
    recent = [r for r in risky if r["checkpoint"].get("checkpoint_age_seconds") is not None and r["checkpoint"]["checkpoint_age_seconds"] <= 600]
    resumable = [r for r in risky if r["checkpoint"]["cold_resume_available"]]
    joint = [r for r in recent if r["checkpoint"]["cold_resume_available"]]
    report["continuity_protection"] = {
        "risky_checkpoints": len(risky),
        "recent_checkpoint_coverage": {**_rate(len(recent), len(risky)), "descriptive_only": True},
        "cold_resume_readiness": {**_rate(len(resumable), len(risky)), "descriptive_only": True},
        "joint_protection": {**_rate(len(joint), len(risky)), "descriptive_only": True},
        "recent_checkpoint_definition_seconds": 600,
    }
    return report


def split_corpus(corpus: dict, train_fraction: float, seed: int) -> tuple[dict, dict]:
    if not 0 < train_fraction < 1:
        raise ValueError("train fraction must be between zero and one")
    sessions = list(corpus["sessions"])
    if len(sessions) < 2:
        raise ValueError("at least two sessions are required to split")
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    signature_group: dict[str, str] = {}
    for session in sessions:
        signature = _signature(session)
        group_id = signature_group.setdefault(signature, session["source_group_id"])
        grouped[session["scenario"]][group_id].append(session)
    rng = random.Random(seed)
    train_sessions: list[dict] = []
    eval_sessions: list[dict] = []
    for scenario in sorted(grouped):
        units = list(grouped[scenario].values()); rng.shuffle(units)
        if len(units) == 1:
            (train_sessions if len(train_sessions) <= len(eval_sessions) else eval_sessions).extend(units[0])
            continue
        target = train_fraction * sum(len(unit) for unit in units)
        scenario_train: list[dict] = []
        scenario_eval: list[dict] = []
        used = 0
        for idx, unit in enumerate(units):
            remaining = len(units) - idx
            choose_train = used < target and remaining > 1
            (scenario_train if choose_train else scenario_eval).extend(unit)
            used += len(unit) if choose_train else 0
        train_sessions.extend(scenario_train); eval_sessions.extend(scenario_eval)
    if not train_sessions or not eval_sessions:
        raise ValueError("split could not produce non-empty train and evaluation sets")
    def pack(part: list, role: str) -> dict:
        meta = {"split_role": role, "split_seed": seed}
        if corpus.get("corpus", {}).get("corpus_id"):
            meta["corpus_id"] = corpus["corpus"]["corpus_id"]
        return {"schema_version": 1, "corpus": meta, "sessions": part}
    train, evaluation = pack(train_sessions, "train"), pack(eval_sessions, "evaluation")
    assert_disjoint(train, evaluation)
    return train, evaluation


def markdown(report: dict) -> str:
    lines = ["# Compaction timing evaluation", "",
             f"Corpus: **{report['corpus_label']}**. Sessions: **{report['sessions']}**. Checkpoints: **{report['checkpoints']}**.", ""]
    if report["corpus_label"].startswith("synthetic"):
        lines += ["> Limitation: Synthetic challenge cases test policy behavior and evaluator integrity; they do not estimate production prevalence. Run this evaluator on private, session-grouped real data before changing runtime policy.", ""]
    lines += ["## Overall advisory results", "",
              "| Policy | Recommendations | Boundary precision | Boundary recall | Product-truth precision |",
              "|---|---:|---:|---:|---:|"]
    def pct(rate: dict) -> str:
        return "n/a" if rate["value"] is None else f"{rate['value']*100:.1f}% ({rate['numerator']}/{rate['denominator']})"
    for name in POLICIES:
        m = report["policies"][name]
        lines.append(f"| {name} | {m['recommendations']} | {pct(m['safe_boundary_precision'])} | {pct(m['safe_boundary_recall'])} | {pct(m['product_truth_precision'])} |")
    lines += ["", "## By occupancy band", "", "| Band | Policy | Precision | Recall | Product truth |", "|---|---|---:|---:|---:|"]
    for band, policies in report["by_occupancy_band"].items():
        for name in POLICIES:
            m = policies[name]
            lines.append(f"| {band} | {name} | {pct(m['safe_boundary_precision'])} | {pct(m['safe_boundary_recall'])} | {pct(m['product_truth_precision'])} |")
    c = report["continuity_protection"]
    lines += ["", "## Continuity protection", "",
              f"Among {c['risky_checkpoints']} risky checkpoints, recent checkpoint coverage was {pct(c['recent_checkpoint_coverage'])}, cold-resume readiness was {pct(c['cold_resume_readiness'])}, and joint protection was {pct(c['joint_protection'])}.", "",
              "## Interpretation", "",
              "`current_advisory` is an observed decision for real data. On this synthetic corpus only, it is the canonical-Python-default approximation and not cross-host runtime truth. `semantic_boundary` is a challenger. `hybrid` requires both. Progressive checkpoint coverage is reported separately because protection is not a recommendation to compact.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run"); run.add_argument("--corpus", required=True); run.add_argument("--train-corpus")
    run.add_argument("--json", action="store_true"); run.add_argument("--output")
    split = sub.add_parser("split"); split.add_argument("--corpus", required=True); split.add_argument("--train-output", required=True)
    split.add_argument("--eval-output", required=True); split.add_argument("--train-fraction", type=float, default=.7); split.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        if args.command == "split":
            train, evaluation = split_corpus(load_corpus(args.corpus), args.train_fraction, args.seed)
            for path, value in ((args.train_output, train), (args.eval_output, evaluation)):
                Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            return 0
        corpus = load_corpus(args.corpus); train = load_corpus(args.train_corpus) if args.train_corpus else None
        report = evaluate(corpus, train)
        rendered = json.dumps(report, indent=2) + "\n" if args.json else markdown(report)
        if args.output: Path(args.output).write_text(rendered, encoding="utf-8")
        else: print(rendered, end="" if rendered.endswith("\n") else "\n")
        return 0
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        print(f"compaction-timing-eval: {exc}", file=sys.stderr); return 2

if __name__ == "__main__":
    raise SystemExit(main())
