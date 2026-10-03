// Token Optimizer's files and the engine's usage, read into the band's shapes
// (U7). Pure and forgiving: a half-written or foreign file reads as nothing
// rather than throwing, so one bad read never blanks the band.
import type { Limit, Quality, Savings } from './contracts.ts'

/** How fresh `measure.py status-bar`'s savings are (KTD5). */
export type SavingsState = 'fresh' | 'stale' | 'loading' | 'unavailable'

/** The last measured cache-write lifetime on the main thread (R10). */
export type CacheLifetime = '1h' | '5m'

/** `measure.py status-bar --json`, reduced to what the band uses. Epochs in seconds. */
export type StatusBar = {
  savings: Savings | null
  savingsState: SavingsState
  /** One short reason when savings is null (R6). */
  savingsReason: string | null
  lastRequestEpoch: number | null
  cacheLifetime: CacheLifetime | null
  checkpointEpoch: number | null
}

/** `$.session.usage()`, reduced to fill and limits (R16). */
export type UsageView = {
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number | null
  fiveHour: Limit | null
  week: Limit | null
}

/** Where one Token Optimizer install keeps measure.py, and its fast launcher when it has one. */
export type TokenOptimizerRoot = {
  scriptsDir: string
  /** hooks/module_runner.py in a plugin install; null for the skill install. */
  runner: string | null
}

/** Days of savings bars (R6). */
export const SAVINGS_DAYS = 30

/** Epochs this far past now are clock skew or milliseconds, not a real time. */
const FUTURE_SLACK_S = 300

/** The waste signals in a quality cache's breakdown, by the words the band shows. */
const DRAG_PHRASES: Readonly<Record<string, string>> = {
  bloated_results: 'bloated tool results',
  stale_reads: 'stale file reads',
  duplicates: 'repeated system reminders',
  reread_loops: 'files re-read in loops',
}

type Json = Record<string, unknown>

function toRecord(input: unknown): Json | null {
  let value = input

  if (typeof input === 'string') {
    try {
      value = JSON.parse(input)
    } catch {
      return null
    }
  }

  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : null
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function count(value: unknown): number | null {
  const n = num(value)

  return n === null ? null : Math.max(0, Math.floor(n))
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : null
}

function epoch(value: unknown, nowEpoch: number): number | null {
  const n = num(value)

  return n !== null && n > 0 && n <= nowEpoch + FUTURE_SLACK_S ? n : null
}

/** Token Optimizer's `score_to_grade` bands, for a cache that left the grade out. */
function gradeFor(score: number): string {
  if (score >= 90) return 'S'
  if (score >= 80) return 'A'
  if (score >= 70) return 'B'
  if (score >= 55) return 'C'
  if (score >= 40) return 'D'

  return 'F'
}

function dragFrom(breakdown: unknown): string | null {
  const signals = toRecord(breakdown)

  if (!signals) {
    return null
  }

  let worst: { phrase: string; tokens: number } | null = null

  for (const [key, phrase] of Object.entries(DRAG_PHRASES)) {
    const tokens = num(toRecord(signals[key])?.estimated_waste_tokens) ?? 0

    if (tokens > 0 && (!worst || tokens > worst.tokens)) {
      worst = { phrase, tokens }
    }
  }

  return worst?.phrase ?? null
}

/**
 * A `quality-cache-<session>.json`, raw text or parsed. Resource health is the
 * score when present (it is what Token Optimizer headlines), else the legacy
 * score; null when neither is a number.
 */
export function parseQualityCache(json: unknown, nowEpoch: number): Quality | null {
  const cache = toRecord(json)

  if (!cache) {
    return null
  }

  const health = num(cache.resource_health)
  const score = health ?? num(cache.score)

  if (score === null) {
    return null
  }

  const grade = (health !== null ? text(cache.resource_health_grade) : null) ?? text(cache.grade) ?? gradeFor(score)

  return {
    score,
    grade,
    drag: dragFrom(cache.breakdown),
    toolCalls: count(cache.tool_calls),
    compactions: count(cache.compactions) ?? 0,
    checkpointEpoch: epoch(cache.last_checkpoint_epoch, nowEpoch),
    sessionStartEpoch: epoch(cache.session_start_ts, nowEpoch),
  }
}

function dailyTokens(daily: unknown): number[] {
  const days = Array.isArray(daily) ? daily.slice(-SAVINGS_DAYS) : []
  const tokens = days.map(entry => Math.max(0, num(toRecord(entry)?.tokens) ?? 0))

  return [...Array<number>(SAVINGS_DAYS - tokens.length).fill(0), ...tokens]
}

function parseSavings(value: unknown): Savings | null {
  const savings = toRecord(value)

  if (!savings) {
    return null
  }

  return {
    sessionTokens: count(savings.session_tokens),
    last30Tokens: count(savings.total_30d_tokens),
    daily: dailyTokens(savings.daily),
  }
}

const STATES: readonly SavingsState[] = ['fresh', 'stale', 'loading', 'unavailable']

/** `measure.py status-bar --json` stdout, raw text or parsed; null when it is not a JSON object. */
export function parseStatusBar(json: unknown): StatusBar | null {
  const out = toRecord(json)

  if (!out) {
    return null
  }

  const state = STATES.find(s => s === out.savings_state) ?? 'unavailable'
  const lifetime = out.cache_lifetime === '1h' || out.cache_lifetime === '5m' ? out.cache_lifetime : null
  const lastRequest = num(out.last_request_epoch)
  const checkpoint = num(out.last_checkpoint_epoch)

  return {
    savings: parseSavings(out.savings),
    savingsState: state,
    savingsReason: text(out.savings_reason),
    lastRequestEpoch: lastRequest !== null && lastRequest > 0 ? lastRequest : null,
    cacheLifetime: lifetime,
    checkpointEpoch: checkpoint !== null && checkpoint > 0 ? checkpoint : null,
  }
}

function limit(limits: unknown, kind: string): Limit | null {
  if (!Array.isArray(limits)) {
    return null
  }

  const found = limits.map(toRecord).find(entry => entry?.kind === kind)
  const percentUsed = num(found?.percentUsed)

  return found && percentUsed !== null ? { percentUsed, resetsAt: text(found.resetsAt) } : null
}

/** `$.session.usage()`'s `{ context, rateLimits }`; anything missing reads as null. */
export function parseUsage(usage: unknown): UsageView {
  const all = toRecord(usage)
  const context = toRecord(all?.context)
  const tokens = num(context?.tokens)
  const window = num(context?.window)
  const percent = num(context?.percent) ?? (tokens !== null && window ? (tokens / window) * 100 : null)

  return {
    contextPercent: percent,
    contextTokens: tokens,
    contextWindow: window !== null && window > 0 ? window : null,
    fiveHour: limit(all?.rateLimits, 'five_hour'),
    week: limit(all?.rateLimits, 'seven_day'),
  }
}

function trimSlash(path: string): string {
  return path.replace(/[\\/]+$/, '')
}

/**
 * Where to look for measure.py, in order (KTD6): every `token-optimizer@<market>`
 * install in `~/.claude/plugins/installed_plugins.json`, newest first, then
 * the skill install `~/.claude/skills/token-optimizer`. The caller keeps the
 * first whose measure.py exists.
 */
export function resolveTokenOptimizerRoot(installedPluginsJson: unknown, home: string): TokenOptimizerRoot[] {
  const plugins = toRecord(toRecord(installedPluginsJson)?.plugins)
  const installs: { path: string; updated: number }[] = []

  for (const [id, entries] of Object.entries(plugins ?? {})) {
    if (!id.startsWith('token-optimizer@') || !Array.isArray(entries)) {
      continue
    }

    for (const entry of entries) {
      const record = toRecord(entry)
      const path = text(record?.installPath)

      if (path) {
        const updated = Date.parse(text(record?.lastUpdated) ?? '')
        installs.push({ path: trimSlash(path), updated: Number.isNaN(updated) ? 0 : updated })
      }
    }
  }

  const roots: TokenOptimizerRoot[] = installs
    .sort((a, b) => b.updated - a.updated)
    .map(({ path }) => ({ scriptsDir: `${path}/skills/token-optimizer/scripts`, runner: `${path}/hooks/module_runner.py` }))

  if (home) {
    roots.push({ scriptsDir: `${trimSlash(home)}/.claude/skills/token-optimizer/scripts`, runner: null })
  }

  return roots.filter((root, i) => roots.findIndex(other => other.scriptsDir === root.scriptsDir) === i)
}
