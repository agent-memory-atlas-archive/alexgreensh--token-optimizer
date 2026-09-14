# Token Optimizer: Benchmark Report

This report shows what Token Optimizer saved, how every number was measured, and how you reproduce each one on your own data. The figures are one user's **last 30 days** (2026-08-15 to 2026-09-14, snapshot 2026-09-14); yours will differ, and every tool to regenerate them ships in the repo.

> This is the **real-world** report — actual usage over 30 days, all six levers on. Two controlled experiments sit beside it and are never added to it: the **Terminal-Bench floor** (the full 81-task Terminal-Bench 2 suite, plugin on vs off, multi-trial with confidence intervals) and the **controlled A/B** that isolates each compression mechanism ([`CONTROLLED-A-B-BENCHMARK.md`](./CONTROLLED-A-B-BENCHMARK.md)). Three different lenses, not competing numbers. The docs site carries all three: [token-optimizer.dev/reference/benchmarks](https://token-optimizer.dev/reference/benchmarks/).

---

## What it saved (30 days)

Three tiers, labelled by how each is known. One is **directly metered** (every event logged as it happened). The other two are **estimates**, modelled from what happened and shown separately.

| Tier | 30 days | How it is known |
|---|---|---|
| 🟢 **Logged actions** | **$126** | Metered. Each saving logged as it happened, with before and after token counts. 2,733 events. |
| 🔵 **Repeat reads avoided** | **$1,124** | Estimated. Removed content that would have been re-read on every later turn until the session's next real compaction. |
| 🔵 **Other estimates** | **$146** | Estimated. Lean session resumes, compressed sub-agent context, MCP output caps, loops prevented. |
| **Total** | **≈ $1,396** | API-equivalent value for the month. |

In tokens, for the same month: **59.6M never sent to the model**, about **28%** of the workload. The month cost 150.8M tokens (fresh input plus de-duplicated output); the same work without Token Optimizer would have cost about 210.4M. The saving splits into 25.4M metered and 34.2M estimated. Tokens are the primary figure; dollars depend on which models you run and are shown second for that reason.

---

## How we measured everything

Each number above comes from a specific, reproducible method. Here is each one.

<details>
<summary>Logged actions, $126, metered event by event</summary>

Every time Token Optimizer shrinks, evicts, or blocks an output, it writes a savings event with the real before/after token counts. Dollars are repriced to each session's own model mix. Over the last 30 days: **2,733 events, 25.43M tokens removed, $126.00**.

| Mechanism | Events | Tokens removed | Saved |
|---|---|---|---|
| Tool-output archive | 1,113 | 18.80M | $100.09 |
| Archive re-fetch blocked | 103 | 2.38M | $12.71 |
| Lean-output nudge (measured) | 291 | 1.21M | $6.42 |
| Bash output compression (pipelines, git, generic) | 883 | 2.73M | $5.83 |
| Loop detection | 124 | 0.12M | $0.35 |
| Checkpoint restores | 113 | 0.08M | $0.34 |
| Cross-turn dedup | 23 | 0.11M | $0.21 |
| Structure maps (re-reads) | 4 | 9.5K | $0.05 |
| Delta reads | 5 | −0.5K | −$0.00 |
| Quality nudges | 74 | 0 | $0.00 |
| **Total** | **2,733** | **25.43M** | **$126.00** |

Delta reads ran at a small net loss this month (five diffs longer than the file). Reported as measured, not hidden.

The lean-output nudge is metered within each session: every nudge records the pre-nudge average output tokens, then the actual post-nudge output is measured against it as `max(0, pre-nudge average − post-nudge average) × post-nudge turns`. Never an estimate, never negative, never booked before post-nudge turns exist.

Reproduce: `python3 scripts/measure.py savings --json` (your numbers will differ; this is one 30-day window)

</details>

<details>
<summary>Repeat reads avoided, $1,124, an estimate capped at real compaction events</summary>

When Token Optimizer removes content from context, that content would otherwise have been re-sent on every later turn of the session. Each removal is multiplied by the de-duplicated number of turns until that session's **next real compaction event**, priced at the per-turn cache-read rate the session actually paid, and stops at the compaction. Only metered-magnitude removals count; lean resumes and other estimated-magnitude events are excluded so nothing is counted twice.

| Measure | 30 days |
|---|---|
| Removals counted | 2,153 |
| Tokens removed at the time | 20.5M |
| Re-read tokens avoided on later turns | 1.99B |
| Value at each turn's cache-read rate | $1,124.13 |

Reproduce: `python3 scripts/measure.py dashboard` (the "counted repeat reads" card)

</details>

<details>
<summary>Other estimates, $146, each listed</summary>

| Source | Volume | Tokens | Saved |
|---|---|---|---|
| Lean session resumes (cold reload avoided) | 156 resumes | 31.85M | $105.16 |
| Sub-agent runtime compression | 846 dispatches | 5.60M | $27.99 |
| MCP output cap | 466 capped results | 2.33M | $11.72 |
| Loops prevented (continuations not run) | 131 events | 0.13M | $0.67 |
| Hint-served reads | 2 hints used | 10K | $0.05 |
| **Total** | | | **$145.60** |

Reproduce: `python3 scripts/measure.py savings --json` (the `*_estimated`, `uncaptured_runtime` and `behavioral_estimate` blocks)

</details>

<details>
<summary>Compression safety, the 87-fixture suite</summary>

Before trusting compression on real output, we validate it never removes what the model needs. The suite holds **87 fixtures across 22 categories, all passing**. Each fixture defines raw output, a must-preserve list, a must-not-contain list (catches hallucination), and a minimum ratio. It passes only when all three hold.

| Category | # | What's tested |
|---|---|---|
| test_exts | 10 | test-runner variants across file extensions |
| build | 8 | cargo, make, webpack, tsc, gradle |
| git | 7 | status, log, diff, merge conflicts |
| lint | 7 | eslint, ruff, clippy, pylint |
| logs | 7 | nginx, docker, systemd, application |
| 🔄 tee_on_failure | 5 | failed commands keep full output |
| json | 4 | value-preserving structured compression |
| csv | 4 | columnar compression |
| stack_trace | 4 | frame-limited tracebacks |
| cloud_cli | 4 | aws, gcloud, az output |
| search_results | 4 | grep and web search condensed to top hits |
| 🔒 security | 3 | AWS keys, GitHub PATs, Slack tokens (must NOT be stripped) |
| tree | 3 | nested directory structures |
| progress | 3 | download and install progress bars |
| list | 3 | long listings |
| k8s | 3 | kubectl output |
| test_runner | 2 | pytest, jest, go test |
| ⚠️ error | 2 | non-zero exit, permission denied (must pass through raw) |
| package_install | 1 | pip and package managers |
| directory_listing | 1 | large `ls` output |
| npm_install | 1 | npm ci and install |
| test | 1 | generic test output |

The security, error and tee-on-failure groups are the load-bearing guardrails. Compression never costs you a credential, an error message, or the output of a failed command. In long sessions (more than 50 messages) the prompt-cache hit rate is slightly higher with archives on (0.969) than off (0.958), because archive-to-stub keeps the cached prefix stable.

Reproduce: `python3 scripts/benchmark.py`

</details>

<details>
<summary>Trust tiers, why we never sum estimates with metered dollars as one number</summary>

- **Measured**: directly metered, each saving logged as it happened with before and after token counts.
- **Estimated**: modelled from what happened (repeat reads avoided, lean resumes, sub-agent context), shown separately and labelled every time.
- **Opportunity**: realizable if you act on a recommendation, never folded into a headline.

Prompt-cache reads are never claimed as standalone savings, because the cache is free infrastructure. Where an estimate uses cache-read rates, it is priced at the rate the turn actually paid and stops at the session's next real compaction.

</details>

<details>
<summary>What is excluded</summary>

The benchmark harness writes its own test-fixture events into the same local ledger. In this window that was 766 events (7.2M tokens, $14.47) tagged `f3bench-session-0001` and `test-session-*`, recorded between 2026-08-31 and 2026-09-06. They are excluded from every figure in this report.

</details>

---

## How to measure your own

Every figure above regenerates against your own session history. Results will differ, and that is the point.

```bash
# Your headline plus the three tiers (the dashboard)
python3 scripts/measure.py dashboard

# The savings ledger behind the tiers
python3 scripts/measure.py savings
python3 scripts/measure.py savings --json

# Just the live compression numbers
python3 scripts/measure.py compression-stats
python3 scripts/measure.py compression-stats --days 7 --json

# The 87-fixture compression suite (deterministic)
python3 scripts/benchmark.py
python3 scripts/benchmark.py --json

# First-read skeleton analysis (historical corpus)
python3 scripts/compression_backfill.py

# Cohort status and tripwire state
python3 scripts/measure.py cohorts status --json
```

---

## Corpus (last 30 days, snapshot 2026-09-14)

| | |
|---|---|
| Sessions | **1,698** main sessions, plus 104 sub-agent sidechains |
| Quality-scored sessions, all-time | **5,210** (ledger begins 2026-05-30) |
| Logged savings events | **2,733** (30 days) |
| Tokens removed, metered | **25.43M** (30 days) |
| First-reads analyzed | **49,886** across 9,801 sessions (historical skeleton corpus) |
| Compression fixtures | **87** across 22 categories, all passing |
| Avg prompt-cache hit rate | **67.2%** |

Production figures come from Claude Code CLI sessions (the author's primary platform). Quality scoring and savings tracking run on all supported platforms; signal counts vary by platform (3 to 7).

---

## More detail

<details>
<summary>First-read skeletons (code only, all four code cohorts currently tripwired)</summary>

Large **code** files read for the first time and unlikely to be edited soon can be served as a skeleton, with the full original archived and recoverable via `expand`, a ranged Read, or a direct Edit. A file type is only promoted to active serving after proof from real history: edit-within-5-turns under 15%, across 20 or more reads in 5 or more sessions.

Measured across the historical first-read corpus (9,801 sessions, 49,886 first reads):

| Language | Size | First reads | Edit rate (history) | Skeleton ratio | Would save |
|---|---|---|---|---|---|
| python | 16-64KB | 1,167 | 0.9% | 96.0% | 9.7M |
| python | 64-256KB | 66 | 1.5% | 98.5% | 1.5M |
| typescript | 16-64KB | 522 | 0.6% | 97.7% | 4.5M |
| markdown | 16-64KB | 2,962 | 2.2% | 97.4% | 24.5M (measure-only by policy) |

A live tripwire watches every active cohort; if its real edit-after-skeleton rate crosses 15%, it auto-demotes to measure-only and logs a `cohort_demoted` event. As of this snapshot the tripwire has demoted all four code cohorts: live edit-after-skeleton rates of 16.0% (Python 16-64KB), 16.7% (Python 64-256KB), 50.0% (TypeScript 16-64KB) and 40.0% (TypeScript 64-256KB). The history backfill still rates them promotion-ready on all-time edit rates, which is exactly the disagreement the tripwire exists to catch: live behaviour wins. Markdown stays measure-only by policy, because a headings-only skeleton drops load-bearing prose. The first-read **retarget** that shipped in v2 serves the file you opened in full and moves the compression budget to skeletons of the related files around it. The full original is always archived first (fail-open: if archiving fails, the full file is served unchanged).

</details>

<details>
<summary>Session quality grades (1,698 sessions, 30 days)</summary>

Every session is scored on 7 signals (context-fill degradation, stale reads, bloated results, compaction depth, decision density, agent efficiency, absolute waste) and graded S through F.

| Grade | Sessions | |
|---|---|---|
| S | 714 | Exceptional |
| A | 210 | Good |
| B | 463 | Normal |
| C | 77 | Degraded, coaching suggested |
| D | 162 | Poor, heavy bloat or retries |
| F | 72 | Failing, near-total waste |

All 5,210 sessions are graded all-time on the same scale, so grades compare across hosts.

</details>

<details>
<summary>Token counting and known measurement gaps</summary>

Token counts use a `bytes / 4` BPE proxy (about 15% error versus actual Claude tokenization), applied consistently so ratios hold. The Terminal-Bench and controlled A/B reports count real billed tokens from session transcripts instead.

- **Opus fast-mode is under-counted by about 50%.** Fast mode (2x rate) is not exposed in session logs, so fast-mode sessions are priced at the standard rate until the transcript exposes it.
- **Older Fable 5 sessions recorded $0.** Trend views compute Fable cost at query time, so trend figures are authoritative even where an older stored per-session value reads $0.
- **Cache-health waste is an opportunity-tier heuristic.** Run `cache-report --verbose` to trace any figure to the sessions behind it.
- **Sub-agent savings are a platform gap outside Claude Code.** OpenClaw and OpenCode have no Claude-style sidechain transcripts, so that pool reads zero there rather than being estimated.

</details>
