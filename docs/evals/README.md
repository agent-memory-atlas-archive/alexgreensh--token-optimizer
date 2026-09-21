# Compaction timing evaluation

This evaluator measures when Token Optimizer should advise a compact or fresh start, and separately measures whether its checkpoint/cold-resume protection is ready when context becomes risky.

```bash
python3 scripts/compaction_timing_eval.py run \
  --corpus tests/fixtures/compaction-timing/challenge-v1.json
```

Use `--json` for machine-readable output and `--output FILE` to save either format.

## Real-session workflow

The committed challenge matrix is synthetic by design. It tests policy behavior and evaluator integrity; it does not estimate production prevalence. For a production estimate:

1. Export candidate checkpoints from local transcripts. Do not commit raw transcripts.
2. Annotate each checkpoint with `safe_boundary` using only information visible at that checkpoint.
3. Inspect the next observed user turn and label `next_turn_needed_older_context`. Use `null` when it cannot be determined.
4. Mark provenance `real` or `sanitized_real`. Scrub message content because the evaluator only needs structured labels and telemetry.
5. Split by whole session, then evaluate:

```bash
python3 scripts/compaction_timing_eval.py split \
  --corpus /private/annotated-sessions.json \
  --train-output /private/train.json \
  --eval-output /private/eval.json \
  --seed 42

python3 scripts/compaction_timing_eval.py run \
  --train-corpus /private/train.json \
  --corpus /private/eval.json --json
```

The run fails if any session ID appears in both files. Confidence intervals resample whole sessions rather than individual checkpoints.

## Corpus format

The file contains metadata plus whole sessions. Raw message text is neither required nor accepted:

```json
{
  "schema_version": 1,
  "corpus": {"name": "private run", "limitations": "how this sample was selected"},
  "sessions": [{
    "session_id": "stable-anonymous-id",
    "host": "claude",
    "scenario": "mid_task_coding",
    "provenance": "sanitized_real",
    "checkpoints": [{
      "checkpoint_id": "turn-42",
      "occupancy_pct": 78,
      "quality_score": 61,
      "compaction_depth": 0,
      "settled": true,
      "completion_cue": false,
      "pending_work": true,
      "checkpoint_age_seconds": 240,
      "cold_resume_available": true,
      "safe_boundary": false,
      "next_turn_needed_older_context": true,
      "policy_observed": {"current_advisory": true}
    }]
  }]
}
```

`scenario` is one of `mid_task_coding`, `coordination`, `blocked_work`, `agent_fanout`, or `long_supervision`. `provenance` is `real`, `sanitized_real`, or `synthetic`. Use `null` for unknown product truth and omit `policy_observed` when replaying the documented approximation instead of an observed decision.

## Policy meanings

- `current_advisory`: TO's fresh-session recommendation gate (fill at least 45% and quality below 70) plus its 90% `/compact` or `/clear` warning, reconstructed from telemetry unless `policy_observed.current_advisory` records what actually happened.
- `semantic_boundary`: a deterministic challenger based on settled state, an explicit completion cue, and no pending work.
- `hybrid`: the current risk gate and the semantic boundary must both qualify.
- `continuity_protection`: recent progressive checkpoint and cold-resume readiness. This is reported separately because protection is not a recommendation to compact.

Use `policy_observed` for historical replay whenever an observed decision is available. Reconstruction is a documented approximation, not a claim about emitted production behavior.

## Reading the report

Boundary precision answers: "When this policy advises action, was the unit actually at a safe boundary?" Boundary recall answers: "How many safe boundaries did it catch?" Product-truth precision asks whether the next observed user turn avoided needing older context. None of these is a reason to change runtime policy without an adequately sized, session-grouped real corpus.
