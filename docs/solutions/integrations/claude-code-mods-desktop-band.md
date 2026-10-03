---
title: Claude Code mods for the desktop status bar, verified behaviour
category: integrations
component: token-optimizer-desktop
runtime: claude-code
severity: medium
verified: true
---

# Claude Code mods for the desktop status bar: verified behaviour

Observed on Claude Code 2.1.288 (CLI, Claude Max, Haiku 4.5), 2026-10-03, with a throwaway mod loaded through `--plugin-dir`. Desktop-only behaviour (animation paint, Client modules, hover cards, reduced motion) still needs a desktop check.

## Problem

The mod API declarations leave several behaviours the status bar depends on unstated: whether a fork warms the main cache, whether a plugin may run `/clear`, which events fire around compaction and clear, and what usage figures a mod sees.

## What we observed

- **`$.model.fork` reads the whole cached conversation and adds no message rows.** The fork's `cache_read_input_tokens` (42,887) covered the main thread's last request (42,240 read plus 584 written), with zero cache writes. The transcript gained only the command's own system rows. A fork is therefore a valid Keep warm: a cache read refreshes that entry's lifetime.
- **`turn.complete.usage` is summed over every request in the turn.** One turn with a tool call reported 67,607 read and 17,457 written while each request carried about 42,000. Anything that needs the context size or the cache anchor must use per-request usage (the transcript's assistant rows, or `turn.step` stop chunks), never the turn sum.
- **The cache lifetime is visible in the transcript.** Assistant rows carry `cache_creation.ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`. On Claude Max every write was one-hour. `turn.complete.usage` does not carry this split.
- **`$.session.compact()` works from a command hook when run outside the hook** (scheduled with `$.clock.after`). Token Optimizer's classic `PreCompact` fires for a plugin-triggered compaction, and the classic `SessionStart` with source `compact` injects Token Optimizer's recovery. So the mod passes no instructions of its own.
- **`$.command.run({ command: 'clear' })` is allowed for a plugin** when run outside the calling hook. It fires `session.end` with reason `clear`, then the classic `SessionStart` with source `clear`, whose `additionalContext` carries Token Optimizer's "Cross-session checkpoint" pointer. No `session.start` fires afterwards.
- **`$.session.usage()`** returns `{ startedAt, context: { tokens, window, percent }, rateLimits: [{ kind: 'five_hour' | 'seven_day', percentUsed, resetsAt }], cost }`.
- **Validator rule:** `$` may be passed only to functions declared at the top of the module (a function declaration or a const bound to one). Helpers defined inside `register` that take `$` fail `claude plugin validate`.
- **One hooks module per plugin, and `$` never crosses an import.** A two-entry `modules` list is refused. `$.env.get` takes only literal variable names, and `atom()` needs a literal `{ plugin, key }` in the hooks module itself. Shared logic therefore takes a small adapter built from `$` inside the hooks module, which also makes it testable under plain Node.
- **`PluginState` is keyed by plugin, then by atom key** (`PluginState['token-optimizer-desktop'].session`).
- **Plugin tests:** `claude plugin test` runs every `*.test.ts(x)` inside the engine, where `node:test` cannot be imported. Pure Node tests therefore use `*.spec.ts`.
- **Rollout switch:** a stale "switched off" state cached by an earlier session blocks `claude plugin test` until any `claude` run refreshes it with network access.

## Still to verify on the desktop app

- SMIL inside an interactive `Svg` animates; CSS `@keyframes` in a `<style>` survives; SMIL in image mode.
- `Client` modules run on desktop, with a frame clock and pointer events.
- A hover-revealed box positioned above the band is not clipped.
- Whether the surface reports reduced motion.
