# Changelog

## [5.13.22] - 2026-09-25

- Fix: the dashboard's daily cost did not match what the requests actually cost, from four separate
  causes (issue #200). Measured against an independent per-request reference built from the same
  transcripts, single days were off by 0.6x to 1.4x before and now match to within a dollar on every
  settled day.
  - Duplicate sessions: one session stored under several transcript paths (a git-worktree project
    dir, a mirror under a sandboxed `CLAUDE_CONFIG_DIR`) was counted once per path, inflating every
    total 2-3x. Collection now keeps one row per session id (the most complete copy) and existing
    duplicate rows are collapsed on the next run. Thanks to @asaarela-bw for the report and patch.
  - Sessions that cross midnight billed every request to their last-active day. Each request is now
    billed to the local calendar day it ran on, and a multi-day session appears on each day it was
    active with that day's share.
  - Subagent spend was left out of cost (it was in the token totals, not the dollars). Daily cost now
    includes every subagent transcript, priced per model.
  - A resumed session kept the date it was first seen, so later spend landed on an old day, often
    outside the window entirely. The session date now follows its latest activity.
  - One API request written into several of a session's files (the parent and a subagent) was
    billed once per file. Requests are now merged by request id across every file of a session,
    and across every copy of it, so copies that diverged lose no spend either. Stored token totals
    use the same merge, so tokens and dollars agree.
  - Requests with no usable timestamp are billed to the session's last active day instead of being
    dropped.
  Rows collected by older versions are backfilled automatically (time-boxed, no rebuild, savings
  history untouched). The dashboard's catch-up writes now wait at most ~200 ms for the database
  and skip when a collector holds it, so a render never stalls behind a flush.

- Fix: Claude Opus 5.5 was priced as Opus 5 ($5/$25) instead of $4/$20 with $0.20 cache reads, and
  Claude Fable 5.1 cache reads were priced at $1 instead of $0.25. Both now use their own rate cards
  in every engine (Claude Code, Codex, OpenClaw, OpenCode, fleet auditor). Labels and savings mixes
  still group them with their family.

- Add: model prices now update themselves. A daily job rebuilds the price table from Anthropic's
  pricing page and the LiteLLM price feed and ships it in a patch release, so new models (including
  new Codex/OpenAI and Gemini models) are priced without a manual code change. The plugin itself
  still makes no network calls: it reads the bundled `pricing/prices.json`. A price moving more than
  2x, dropping to zero, or disappearing opens a pull request for a human instead of shipping. Set
  `TOKEN_OPTIMIZER_BUNDLED_PRICES=0` to use only the built-in table.

- Fix: pricing gaps across engines. OpenClaw now applies the Vertex regional +10% to every Claude
  generation card and bills Gemini requests past 200k prompt tokens at the long-context rate; the
  fleet auditor prices Codex and Hermes sessions by their exact model id; a transcript record whose
  model field is not text no longer aborts collection. Claude 3-era ids (`claude-3-5-sonnet-...`,
  `claude-3-opus-...`, `claude-3-5-haiku-...`) now price at their own rates in every engine instead
  of the current family card (Sonnet 3.x had dropped to the Sonnet 5 rate in OpenClaw, OpenCode and
  the fleet auditor).

- Fix: Hermes, Copilot, Cursor, Grok and Antigravity sessions are dated by their last activity, so a
  resumed chat's spend lands on the day it happened and stays inside the dashboard window.

- Fix: the fleet dashboard server (`fleet.py --serve`) only serves the dashboard page and only to
  localhost host names, so other files in its folder are not readable from a web page.

- Fix: daemon and hook append logs are capped at 1 MB so they cannot grow without bound.

- Fix: opening the dashboard from a terminal UI (OpenCode's `token_dashboard`) let the browser write
  its log output into the TUI. The browser opener now runs fully detached with no inherited
  stdin/stdout/stderr, on every platform path. (issue #199)

## [5.13.16] - 2026-09-17

- Fix: the Hermes context-fill nudge measured the session-CUMULATIVE input tally instead of the live
  prompt, so it reported a context emergency that did not exist. Every host re-sends the whole
  conversation on each turn, so that sum climbs past the model window regardless of real occupancy:
  on a 129-call Hermes session the cumulative figure reached 1,285,803 against a 1,000,000 window
  ("Context ~100% full ... Grade: F", the percentage being capped at 100) while Hermes itself
  reported 278,545 / 1,000,000 = 28% for the same session. The nudge now uses the prompt the last
  call actually sent -- the full prompt_tokens (fresh input + cache-read + cache-write), since every
  prompt token occupies the window -- and says so in the message ("last request prompt ~N tokens vs
  model window M"). The cumulative tally is unchanged for cost and usage reporting. Regression tests:
  `tests/test_hermes_context_fill_nudge.py` (3 of the 5 original cases fail on the previous code,
  plus a cache-write case so the live figure is not undercounted on a cache-creation turn).

- Fix: Bash cross-turn output dedup and the repeat-command thrash nudge were keyed only by session, so
  a subagent could be told its output was "identical to your previous output", or that a command "has
  run N times this session", for work only the main agent had done. Both are now scoped to session plus
  agent identity: a supplied agent id is treated as an opaque identity (never normalized or merged),
  while a missing, empty, or whitespace-only id falls back to the historical session-only identity so
  the main-agent case is unchanged. The thrash guard's edit-detection reads the shared session activity
  log, so a subagent that edits a file between two identical runs still suppresses the false "stuck in a
  loop" nudge. (issue #189)

- Fix: the Windows hook launcher could select an incomplete or unreadable runtime version. It now admits
  only versioned install dirs whose `run.py` actually opens, choosing the newest that does and skipping
  incomplete, unreadable, or zero-byte candidates before falling back to the baked install. This replaces
  a readability check that was a no-op on Windows and removes a case where an unreadable candidate could
  cause the wrong version to be selected on some Python versions. Argv, stdin, and environment
  forwarding are unchanged. (issue #188)

- Fix: `health` and `kill-stale` found no running sessions when Claude Code is launched by the Claude
  Desktop app under WSL2, because the desktop starts a versioned `ccd-cli` binary rather than a `claude`
  binary, so session detection never matched. Detection now also recognizes the ccd-cli launcher via an
  anchored, version-shaped path match on the process command, which excludes bundled helpers and
  unrelated processes so no false sessions are counted. (issue #192)

## [5.13.15] - 2026-09-16

- Fix: `codex_install.py` no longer emits a base64 `python -c` exec-bootstrap as the Windows hook command on versioned marketplace installs (issue #183). The encoded-exec string trips generic-loader antivirus signatures (SentinelOne flagged it as a Metasploit variant, once per shipped copy of the file), even though the payload was fixed, readable, and decode-auditable. The installer now copies a plain, auditable `windows-launcher.py` next to the versioned install dirs -- a stable path that survives marketplace upgrades -- and bakes a command that invokes it by quoted path with plainly quoted argv. Version resolution (newest semver sibling, fail-open to the baked install, TOKEN_OPTIMIZER_DEBUG-gated resolver log), stdin/argv/env passthrough, legacy-command recognition on reinstall/uninstall, and upgrade trust semantics are unchanged: the command signature now normalizes only the `--baked-root` version leaf, legacy base64 commands compare verbatim so the next install replaces them (the one-time review that ships this fix), and `--decode-launcher` still decodes legacy commands while pointing launcher-file commands at their plain source. Regression coverage: an AV-safe source-string guard (the generator and the launcher never contain or emit `exec(`/base64/bootstrap patterns), launcher install/idempotence/stability tests, cross-platform resolver execution tests, and cmd.exe execution tests on Windows CI.
- Fix: the Codex command-compression shell resolver no longer crashes on unreadable PATH entries. A directory the user cannot stat (e.g. another account's private `~/.cargo/bin` on a shared machine) raised PermissionError out of `_default_shell()` and failed the hook; unreadable entries are now skipped like any other non-match.
- Fix: the generated Windows hook command now force-quotes every path/value token. `list2cmdline` only wraps a token containing whitespace, so a space-free install path carrying a cmd.exe metacharacter (`& | < > ^`) was emitted bare and `cmd` parsed it as a command separator; every token is now quoted (cmd treats those as literal inside quotes). A `%` in the install path remains the one documented, unsupported case.

## [5.13.14] - 2026-09-14

- Fix: the Codex log-index now self-heals on Windows instead of crashing on a locked or corrupt database. The open path closes the broken connection before rebuilding (Windows refuses to unlink an open file, so the rebuild previously reconnected to the same corrupt DB), retries each unlink briefly to ride out transient share locks, and no longer misdiagnoses a merely-locked database as corruption and deletes it out from under a live process. Transient lock contention (concurrent first-opens racing journal-mode/schema setup, a writer mid-commit, or a lock-upgrade deadlock that returns BUSY without consulting the busy handler) now retries on a deadline at both the connect and write-transaction stages instead of surfacing "database is locked" to callers -- follow-up hardening on #175.
- Fix: the release installable check no longer races the signing workflow. A fresh release missing CHECKSUMS.sha256 is polled through the signing grace window (measured from published_at) instead of failing instantly -- v5.13.13's check ran at publish+2s while the asset landed ~20s later. An old unsigned release still fails immediately.
- Fix: Codex hooks on native Windows failed with "hook exited with code 1" on versioned marketplace installs. The generated cmd.exe command assigned TOKEN_OPTIMIZER_RUNTIME_ROOT inside a `for /f` loop and read it back with `!TOKEN_OPTIMIZER_RUNTIME_ROOT!` on the same line; cmd parses a /C line once and `setlocal EnableDelayedExpansion` only applies from the next line, so Python received the literal placeholder path. The runner path is now built from the FOR variable `%R` inside the do-body, and the version resolver always prints one directory (newest semver install, else the baked install) so the fallback still runs. Regression tests execute the generated command through `%COMSPEC% /D /C` with spaces in the install path. Reinstall and uninstall now also recognize those broken commands: they carry no `token-optimizer/scripts` path marker (backslash install paths and `hooks/<name>_runner.py` args), so `_is_token_optimizer_group` additionally matches the full signature of our generated command -- the quoted `TOKEN_OPTIMIZER_RUNTIME_ROOT=` assignment together with our `hooks\run.py` runner invocation under a token-optimizer path (or via our own FOR/delayed-expansion variable) -- so a user's own hook that merely references the env var is never swept up. When the version resolver falls back to the baked install it now appends a line to `token-optimizer-codex-resolver.log` next to the version dirs if `TOKEN_OPTIMIZER_DEBUG` is set, making the previously silent fallback observable.

- Fix: the token-saving hooks now reach every supported harness. Codex, Cowork, and manual installs get the same savings as Claude Code -- startup diagnostics stay out of the model's context, and the command-failure and long-output nudges reach the model through each host's supported channel.
- Fix: SessionStart no longer adds anything to the model's context. Startup diagnostics (health checks, dashboard setup, daemon status) now write to a local log file instead of stdout/stderr, both of which the host captures into the session context. Sessions begin at their true baseline, so the token savings start on the first turn.
- Add: burn nudge. When the same command fails 3 times in a row with different output, a nudge suggests changing approach instead of re-running. Catches the edit-compile-fail cycle that the existing identical-output streak guard cannot see. Tunable with `TOKEN_OPTIMIZER_FAIL_STREAK_THRESHOLD` (default `3`).
- Add: inline-script repeat nudge. When a command with a heredoc body >= 300 chars has been run 8 times in a session, a nudge suggests saving the script to a file and running that instead, so the body is not re-sent as input tokens every turn. Tunable with `TOKEN_OPTIMIZER_INLINE_SCRIPT_THRESHOLD` (default `8`).

## [5.13.13] - 2026-09-14

- Add: first-class Codex support, extracted and hardened from external PR #175 by @dormancygrace. Codex sessions get real model pricing (gpt-6-astra, gpt-5.6-sol) through a versioned model catalog, delta-based token accounting that stops the over-count on incremental log writes, canonical session-ids, a SQLite log-index for fast session discovery, native Windows process handling, a security-hardened command-compression hook, and a base64 Windows launcher that survives cmd.exe quoting.
- Add: Codex Token-Coach port with full runtime isolation. Context-window detection, model config, and savings accounting now resolve per-runtime across Claude, Codex, and the five other supported harnesses, so a foreign runtime never inherits Claude's model env vars, ~/.claude config, or the 1M default.
- Fix: coach-integrity hardening across the measurement pipeline. Safe-int/type/size guards reject malformed session records, ANSI/VT escape sequences are stripped before terminal output, log-index reads are confined to the runtime home, concurrent writers no longer corrupt the index, and the consent gate narrows its exception handling and logs unexpected errors so a corrupt config is visible, while still failing open by design.
- Docs: new benchmarks category (Overview, Terminal-Bench floor, Controlled A/B, One real month) with a refreshed real-savings.svg built from current measurements.

## [5.13.10] - 2026-09-08

- Prevent sandbox dashboard tests from replacing the real background service. Isolate hook test homes and cached modules, and keep capped transformation percentages and older marker history consistent.

- Include modeled repeat-read savings in the action card for removals made during the selected period. Retain logged setup, output, routing and unmatched-event savings without counting initial removals twice.
- Make Savings easier to scan: compact transformation and action summaries, explicit periods and estimates, matching percentage and dollar comparisons, and expandable methods that stay open during live refresh.
- Compare lifetime context savings with its matching period subtotal. Preserve previously verified history when transcripts rotate, and remove duplicate delta-read entries from the derived ledger.
- Retain other logged savings in the weekly fallback calculation and show the previously omitted concise-output estimate. Supported runtimes without repeat-read evidence keep their logged totals.

## [5.13.9] - 2026-09-08

- Fix growing session logs being skipped after their first collection. Refresh parent and child activity without duplicating totals or overwriting newer collector results.
- Show the full Savings-tab estimate for the actual subscription week, counting overlapping savings once and labeling the amount as estimated savings accrued so far. Include new sessions before background collection catches up.
- Recover the workload comparison after history backfill or rebuild changes its baseline month. Cache weekly results for up to 60 seconds and retain weekly dollars when quota readings are unavailable.
