# Token Optimizer Desktop

A status bar for the Claude desktop app. It draws above your prompt and shows session quality, context fill, cache countdown, and your 5-hour and weekly limits, with Clawd acting out what the session is doing.

## Install

Alongside the main Token Optimizer plugin:

```
/plugin install token-optimizer-desktop@alexgreensh-token-optimizer
```

Needs Claude Code 2.1.287 or newer and Anthropic's mods feature. Anthropic can switch mods off remotely; when off, nothing breaks and the bar just does not appear. The terminal keeps its existing status line. VS Code is not supported.

## What you get

- One sentence about the most urgent thing, with at most one button.
- Five marks with hover cards: quality grade, context fill, cache countdown, 5-hour limit, weekly limit.
- Click Clawd to unfold branch, session time, tool calls, compactions (when any), last checkpoint, and tokens saved this session and over the past 30 days.
- Buttons: **Clean up** (compacts with Token Optimizer's guidance), **Start fresh** (second click saves a checkpoint, clears, and hands it to your first message), **Keep warm** (manual one-click cache refresh, only while the cache is warm, never automatic, uses a small amount of usage).

The cache countdown is an estimate: 1 hour on Claude plans, 5 minutes on the API, measured from the session itself when possible.

## Settings

`enabled` and `animate`, via `/config`.

## Full docs

See the [Token Optimizer README](../../README.md#desktop-status-bar) and the docs site page "Claude desktop app".
