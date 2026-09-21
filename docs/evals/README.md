# Compaction timing evaluation

This evaluator measures when Token Optimizer should advise a compact or fresh start. It separately measures whether checkpoint/cold-resume protection is ready when context becomes risky.

```bash
python3 scripts/compaction_timing_eval.py run \
  --corpus tests/fixtures/compaction-timing/challenge-v1.json
```

Use `--json` for machine-readable output and `--output FILE` to save either format.

## Real-session workflow

The committed challenge matrix is synthetic. It tests evaluator behavior, not production prevalence. For a production estimate:

1. Export candidate checkpoints locally. Do not commit raw transcripts.
2. Annotate `safe_boundary` using only information visible at that checkpoint.
3. Inspect the next observed user turn and label `next_turn_needed_older_context`; use `null` when unknown.
4. Record the advisory decision that actually occurred as `policy_observed.current_advisory`.
5. Assign a stable anonymous `session_id` and `source_group_id`, then split and run:

```bash
python3 scripts/compaction_timing_eval.py split \
  --corpus /private/annotated-sessions.json \
  --train-output /private/train.json \
  --eval-output /private/eval.json --seed 42

python3 scripts/compaction_timing_eval.py run \
  --train-corpus /private/train.json \
  --corpus /private/eval.json --json
```

The split keeps source groups together and is stratified by scenario. Evaluation fails on overlapping session IDs, source groups, or normalized checkpoint signatures. Confidence intervals resample whole source groups, keeping every related session in the same bootstrap cluster.

## Corpus schema

Raw message text is rejected. Unknown fields fail validation.

| Level | Field | Required | Values / limits |
|---|---|---:|---|
| root | `schema_version` | yes | integer `1` |
| root | `corpus` | no | object; only anonymous `corpus_id` plus generated split fields |
| root | `sessions` | yes | 1-10,000 session objects |
| session | `session_id` | yes | stable anonymous ID, <=128 characters; letters, numbers, `. _ : -` |
| session | `source_group_id` | yes | same format; identical/derived sessions share one group |
| session | `host` | yes | anonymous host label in the same safe ID format |
| session | `scenario` | yes | `mid_task_coding`, `coordination`, `blocked_work`, `agent_fanout`, or `long_supervision` |
| session | `provenance` | yes | `real`, `sanitized_real`, or `synthetic` |
| session | `checkpoints` | yes | non-empty; at most 100,000 total |
| checkpoint | `checkpoint_id` | yes | anonymous ID format |
| checkpoint | `occupancy_pct`, `quality_score` | yes | finite number 0-100; booleans rejected |
| checkpoint | `compaction_depth` | yes | finite nonnegative number; booleans rejected |
| checkpoint | `settled`, `completion_cue`, `pending_work` | yes | boolean |
| checkpoint | `checkpoint_age_seconds` | yes | finite nonnegative number or `null` |
| checkpoint | `cold_resume_available`, `safe_boundary` | yes | boolean |
| checkpoint | `next_turn_needed_older_context` | yes | boolean or `null` |
| checkpoint | `policy_observed` | required for real data | boolean decisions; real rows require `current_advisory` |

Metadata cannot contain descriptions, limitations, labels, or other free text. Split files contain only anonymous `corpus_id`, `split_role`, and `split_seed`. The entire file is limited to 10 MB. Create stable IDs by hashing a private source identifier with a local salt and keeping a short hex digest. Never use names, paths, prompts, or message text as IDs. Use one `source_group_id` for retries, forks, copied sessions, or other related examples that must stay in one partition.

Minimal session shape:

```json
{"schema_version":1,"corpus":{"corpus_id":"private-run"},"sessions":[{
  "session_id":"s-8d91","source_group_id":"g-115a","host":"claude",
  "scenario":"mid_task_coding","provenance":"sanitized_real","checkpoints":[{
    "checkpoint_id":"turn-42","occupancy_pct":78,"quality_score":61,
    "compaction_depth":0,"settled":true,"completion_cue":false,
    "pending_work":true,"checkpoint_age_seconds":240,
    "cold_resume_available":true,"safe_boundary":false,
    "next_turn_needed_older_context":true,
    "policy_observed":{"current_advisory":true}
  }]
}]}
```

## Policy scope

- `current_advisory`: observed historical decision for real data. Synthetic data may use the canonical Python default approximation: fill >=45% and quality <70, or the 90% warning path.
- `semantic_boundary`: settled state + explicit completion cue + no pending work.
- `hybrid`: both current and semantic policies qualify.
- `continuity_protection`: recent checkpoint and cold-resume readiness, reported separately because protection is not an advisory action.

The approximation is not cross-host truth. Python thresholds are configurable and runtime state can suppress a nudge. OpenClaw currently uses a 50% fill floor rather than Python's 45%. Host/version/config comparisons therefore require observed decisions.

## Output and exit codes

- `0`: corpus validated and the requested split or report completed.
- `2`: malformed JSON/schema, empty corpus, size limit, unsafe identifier, or train/evaluation leakage. The CLI prints one deterministic error line, without a traceback.
- Other nonzero codes indicate an unexpected interpreter/runtime failure.

Boundary precision asks whether recommendations were at safe boundaries. Boundary recall asks how many safe boundaries were caught. Product-truth precision asks whether the next turn avoided needing older context. Unknown product truth is excluded and counted. Do not change runtime policy from synthetic results.
