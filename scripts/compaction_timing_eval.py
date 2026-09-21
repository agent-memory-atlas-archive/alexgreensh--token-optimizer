#!/usr/bin/env python3
"""Evaluate compaction/restart timing without changing runtime behavior.

The evaluator keeps task-boundary safety, future context need, and continuity
protection separate. Input corpora are session-grouped JSON; checkpoints from a
session are never split across train/evaluation partitions.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

SCENARIOS = {"mid_task_coding", "coordination", "blocked_work", "agent_fanout", "long_supervision"}
ROOT_KEYS = {"schema_version", "corpus", "sessions"}
SESSION_KEYS = {"session_id", "host", "scenario", "provenance", "checkpoints"}
CHECKPOINT_KEYS = {"checkpoint_id", "occupancy_pct", "quality_score", "compaction_depth", "settled", "completion_cue", "pending_work", "checkpoint_age_seconds", "cold_resume_available", "safe_boundary", "next_turn_needed_older_context", "policy_observed"}
PROVENANCE = {"real", "sanitized_real", "synthetic"}
POLICIES = ("current_advisory", "semantic_boundary", "hybrid")


def occupancy_band(value: float) -> str:
    if value < 50:
        return "<50"
    if value < 70:
        return "50-69"
    if value < 80:
        return "70-79"
    return "80+"


def load_corpus(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    validate_corpus(data, str(path))
    return data


def validate_corpus(data: dict, source: str = "corpus") -> None:
    if data.get("schema_version") != 1 or not isinstance(data.get("sessions"), list):
        raise ValueError(f"{source}: expected schema_version 1 and sessions[]")
    unknown_root = set(data) - ROOT_KEYS
    if unknown_root:
        raise ValueError(f"{source}: unknown root fields: {', '.join(sorted(unknown_root))}")
    seen: set[str] = set()
    for s_idx, session in enumerate(data["sessions"]):
        unknown_session = set(session) - SESSION_KEYS
        if unknown_session:
            raise ValueError(f"{source}: session {s_idx} has unknown fields: {', '.join(sorted(unknown_session))}")
        sid = session.get("session_id")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise ValueError(f"{source}: session {s_idx} has missing or duplicate session_id")
        seen.add(sid)
        if session.get("scenario") not in SCENARIOS:
            raise ValueError(f"{source}: {sid} has unknown scenario")
        if session.get("provenance") not in PROVENANCE:
            raise ValueError(f"{source}: {sid} has unknown provenance")
        checkpoints = session.get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            raise ValueError(f"{source}: {sid} needs at least one checkpoint")
        cp_seen: set[str] = set()
        for c_idx, cp in enumerate(checkpoints):
            unknown_checkpoint = set(cp) - CHECKPOINT_KEYS
            if unknown_checkpoint:
                raise ValueError(f"{source}: {sid} checkpoint {c_idx} has unknown fields: {', '.join(sorted(unknown_checkpoint))}")
            cid = cp.get("checkpoint_id")
            if not isinstance(cid, str) or not cid or cid in cp_seen:
                raise ValueError(f"{source}: {sid} checkpoint {c_idx} has missing/duplicate id")
            cp_seen.add(cid)
            for key in ("occupancy_pct", "quality_score", "compaction_depth"):
                if not isinstance(cp.get(key), (int, float)) or not math.isfinite(cp[key]):
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
            if age is not None and (not isinstance(age, (int, float)) or age < 0 or not math.isfinite(age)):
                raise ValueError(f"{source}: {sid}/{cid} invalid checkpoint age")
            overrides = cp.get("policy_observed", {})
            if not isinstance(overrides, dict) or any(k not in POLICIES or not isinstance(v, bool) for k, v in overrides.items()):
                raise ValueError(f"{source}: {sid}/{cid} invalid policy_observed")


def assert_disjoint(train: dict, evaluation: dict) -> None:
    left = {s["session_id"] for s in train["sessions"]}
    right = {s["session_id"] for s in evaluation["sessions"]}
    overlap = sorted(left & right)
    if overlap:
        raise ValueError("session leakage between train and evaluation: " + ", ".join(overlap[:10]))


def policy_decision(name: str, cp: dict) -> bool:
    observed = cp.get("policy_observed", {})
    if name in observed:
        return observed[name]
    risk = cp["occupancy_pct"] >= 90 or (cp["occupancy_pct"] >= 45 and cp["quality_score"] < 70)
    semantic = cp["settled"] and cp["completion_cue"] and not cp["pending_work"]
    if name == "current_advisory":
        return risk
    if name == "semantic_boundary":
        return semantic
    if name == "hybrid":
        return risk and semantic
    raise ValueError(f"unknown policy {name}")


def _rate(successes: int, total: int) -> dict:
    return {"value": round(successes / total, 4) if total else None, "numerator": successes,
            "denominator": total}


def _cluster_bootstrap(rows: list[dict], name: str, metric: str, samples: int = 2000) -> list[float] | None:
    """Percentile interval from whole-session resampling, never checkpoint resampling."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["session"]["session_id"]].append(row)
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
        "corpus": corpus.get("corpus", {}),
        "sessions": len(corpus["sessions"]), "checkpoints": len(rows),
        "provenance": dict(sorted(Counter(r["session"]["provenance"] for r in rows).items())),
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
    grouped: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        grouped[session["scenario"]].append(session)
    rng = random.Random(seed)
    train_sessions: list[dict] = []
    eval_sessions: list[dict] = []
    for scenario in sorted(grouped):
        group = grouped[scenario]
        rng.shuffle(group)
        if len(group) == 1:
            (train_sessions if len(train_sessions) <= len(eval_sessions) else eval_sessions).extend(group)
            continue
        cut = max(1, min(len(group) - 1, round(len(group) * train_fraction)))
        train_sessions.extend(group[:cut]); eval_sessions.extend(group[cut:])
    if not train_sessions or not eval_sessions:
        raise ValueError("split could not produce non-empty train and evaluation sets")
    def pack(part: list, role: str) -> dict:
        meta = dict(corpus.get("corpus", {})); meta["split_role"] = role; meta["split_seed"] = seed
        return {"schema_version": 1, "corpus": meta, "sessions": part}
    train, evaluation = pack(train_sessions, "train"), pack(eval_sessions, "evaluation")
    assert_disjoint(train, evaluation)
    return train, evaluation


def markdown(report: dict) -> str:
    meta = report["corpus"]
    lines = ["# Compaction timing evaluation", "",
             f"Corpus: **{meta.get('name', 'unnamed')}**. Sessions: **{report['sessions']}**. Checkpoints: **{report['checkpoints']}**.", ""]
    if meta.get("limitations"):
        lines += [f"> Limitation: {meta['limitations']}", ""]
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
              "`current_advisory` represents TO's existing deterministic risk/quality recommendation lane. `semantic_boundary` is a challenger. `hybrid` requires both. Progressive checkpoint coverage is reported separately because protection is not a recommendation to compact.", ""]
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
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"compaction-timing-eval: {exc}", file=sys.stderr); return 2

if __name__ == "__main__":
    raise SystemExit(main())
