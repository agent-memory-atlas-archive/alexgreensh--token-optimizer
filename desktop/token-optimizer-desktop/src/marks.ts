// The five marks (R4), their hover cards (R5) and the detail row (R6) as plain
// data the drawing layer maps to elements. Pure (KTD4).
import type { CacheView, Limit, Snapshot } from './contracts.ts'
import type { IconName } from './icons.ts'
import { ago, clock, duration, minutes, gradeOf, relative, renewal, tokens, type FormatOptions } from './format.ts'
import { LIMIT_WARN, QUALITY_FLOOR, loseRun, type Action, type Run, type Tone } from './ladder.ts'

export type MarkId = 'quality' | 'context' | 'cache' | 'fiveHour' | 'week'
/** 'none' means no data: the drawing layer shows the mark uncoloured. */
export type MarkTone = Tone | 'none'

export type Mark = {
  id: MarkId
  icon: IconName
  /** The figure on the bar, "--" when unavailable. */
  value: string
  label: string
  tone: MarkTone
  /** 0..100 for ring marks (context, cache, limits). */
  ringPercent?: number
  /** The grade letter on the quality mark. */
  badge?: string
  /** Alt text naming the state (R18). */
  alt: string
}

export type Card = { id: MarkId; title: string; body: Run[]; actions: Action[] }

export type Fact = { icon: IconName; runs: Run[] }

export type SavingsBlock = {
  state: 'ready' | 'loading' | 'unavailable'
  sessionTokens: number | null
  last30Tokens: number | null
  sessionText: string
  last30Text: string
  /** Raw daily figures, oldest first, today last; empty when unavailable. */
  daily: number[]
  /** Bar heights in percent of the tallest day, at least 8 so a quiet day still shows. */
  bars: number[]
  /** One short reason when unavailable. */
  reason: string | null
}

export type Row = { facts: Fact[]; savings: SavingsBlock }

const clamp = (p: number) => Math.max(0, Math.min(100, Math.round(p)))
const plain = (text: string): Run => ({ text })
const strong = (text: string): Run => ({ text, strong: true })

export function qualityTone(score: number): Tone {
  return score >= 80 ? 'good' : score >= QUALITY_FLOOR ? 'caution' : 'bad'
}
export function fillTone(p: number): Tone {
  return p >= 80 ? 'bad' : p >= 60 ? 'caution' : 'good'
}
export function limitTone(p: number): Tone {
  return p >= LIMIT_WARN ? 'bad' : p >= 75 ? 'caution' : 'good'
}

function estimateNote(c: CacheView): string {
  return c.measured ? 'an estimate' : 'an estimate, lifetime not measured yet'
}

function cacheMark(c: CacheView): Mark {
  const base = { id: 'cache' as const, label: 'cache' }
  const est = estimateNote(c)
  switch (c.state) {
    case 'warm':
    case 'warning': {
      const left = c.secondsLeft ?? 0
      const tone: Tone = c.state === 'warm' ? 'good' : 'caution'
      const word = c.state === 'warm' ? 'warm' : 'about to drop'
      return { ...base, icon: 'hourglass', value: clock(left), tone, ringPercent: clamp((left / c.lifetime) * 100), alt: `Cache ${word}, ${clock(left)} left, ${est}` }
    }
    case 'cold':
      return { ...base, icon: 'cold', value: 'cold', tone: 'cold', ringPercent: 0, alt: `Cache cold, ${est}` }
    case 'refreshing':
      return { ...base, icon: 'hourglass', value: minutes(c.lifetime), tone: 'good', ringPercent: 100, alt: 'Cache refreshing while the turn runs' }
    case 'warming':
      return {
        ...base,
        icon: 'hourglass',
        value: c.secondsLeft != null ? minutes(c.secondsLeft) : '--',
        tone: 'good',
        ringPercent: c.secondsLeft != null ? clamp((c.secondsLeft / c.lifetime) * 100) : 0,
        alt: 'Cache warm-up running',
      }
    default:
      return { ...base, icon: 'hourglass', value: '--', tone: 'none', ringPercent: 0, alt: 'Cache clock starts after the first reply' }
  }
}

function limitMark(id: 'fiveHour' | 'week', limit: Limit, now: number, opts: FormatOptions): Mark {
  const p = Math.round(limit.percentUsed)
  const name = id === 'fiveHour' ? '5-hour' : 'Weekly'
  const when = renewal(limit.resetsAt, now, opts)
  return {
    id,
    icon: 'clock',
    value: `${p}%`,
    label: id === 'fiveHour' ? '5 hours' : 'week',
    tone: limitTone(limit.percentUsed),
    ringPercent: clamp(limit.percentUsed),
    alt: `${name} limit ${p}% used` + (when ? `, renews ${when}` : ''),
  }
}

/** The marks under the sentence, in order. Limit marks are omitted when the limit is null (R4). */
export function marks(s: Snapshot, opts: FormatOptions = {}): Mark[] {
  const q = s.quality
  const quality: Mark = q
    ? {
        id: 'quality',
        icon: q.score < QUALITY_FLOOR ? 'slip' : 'check',
        value: String(Math.round(q.score)),
        badge: gradeOf(q.score),
        label: 'quality',
        tone: qualityTone(q.score),
        alt: `Quality grade ${gradeOf(q.score)}, score ${Math.round(q.score)} of 100`,
      }
    : { id: 'quality', icon: 'check', value: '--', badge: '--', label: 'quality', tone: 'none', alt: 'Quality not measured yet' }

  const p = s.contextPercent
  const context: Mark =
    p != null
      ? { id: 'context', icon: 'gauge', value: `${Math.round(p)}%`, label: 'context', tone: fillTone(p), ringPercent: clamp(p), alt: `Context ${Math.round(p)}% full` }
      : { id: 'context', icon: 'gauge', value: '--', label: 'context', tone: 'none', ringPercent: 0, alt: 'Context fill not reported yet' }

  const out = [quality, context, cacheMark(s.cache)]
  if (s.fiveHour) out.push(limitMark('fiveHour', s.fiveHour, s.now, opts))
  if (s.week) out.push(limitMark('week', s.week, s.now, opts))
  return out
}

function cacheCard(c: CacheView): Card {
  const est = ` The clock is ${estimateNote(c)}.`
  const warm: Action[] = c.measured ? [{ id: 'warm', label: 'Keep warm' }] : []
  const stake = c.tokensAtStake
  switch (c.state) {
    case 'warm':
      return {
        id: 'cache',
        title: `Warm for ${clock(c.secondsLeft ?? 0)}`,
        body: [plain(stake != null ? `Messages re-read ${tokens(stake)} tokens at a tenth of the price.` : 'Messages re-read the context at a tenth of the price.'), plain(est)],
        actions: warm,
      }
    case 'warning':
      return {
        id: 'cache',
        title: c.secondsLeft != null ? `Drops in ${clock(c.secondsLeft)}` : 'Drops soon',
        body: [
          ...(stake != null ? [plain('Then the next message re-reads '), loseRun(stake), plain(' at full price.')] : [plain('Then the next message re-reads the whole context at full price.')]),
          plain(est),
        ],
        actions: warm,
      }
    case 'cold':
      return {
        id: 'cache',
        title: 'Cold',
        body: [
          ...(stake != null ? [plain('Next message re-reads '), loseRun(stake), plain(' at full price.')] : [plain('Next message re-reads the whole context at full price.')]),
          plain(' Cleaning up first re-reads it once and makes later messages cheaper.'),
        ],
        actions: [{ id: 'clean-first', label: 'Clean up first' }],
      }
    case 'refreshing':
      return { id: 'cache', title: 'Refreshing', body: [plain('Every request in this turn keeps the cache warm.')], actions: [] }
    case 'warming':
      return { id: 'cache', title: 'Keeping warm', body: [plain('A one-line warm-up is running.')], actions: [] }
    default:
      return { id: 'cache', title: 'Not started', body: [plain('The clock starts after the first reply.')], actions: [] }
  }
}

function limitCard(id: 'fiveHour' | 'week', limit: Limit, now: number, opts: FormatOptions): Card {
  const when = renewal(limit.resetsAt, now, opts)
  const at = limit.resetsAt ? Date.parse(limit.resetsAt) : NaN
  if (!when || !Number.isFinite(at)) {
    const name = id === 'fiveHour' ? '5-hour' : 'Weekly'
    return { id, title: `${name} limit ${Math.round(limit.percentUsed)}% used`, body: [plain('Renewal time not reported.')], actions: [] }
  }
  return { id, title: `Renews ${when}`, body: [plain(`That is ${relative(at, now)}.`)], actions: [] }
}

/** Hover card bodies, one per mark, same order and omissions as `marks` (R5). */
export function cards(s: Snapshot, opts: FormatOptions = {}): Card[] {
  const q = s.quality
  const qualityActions: Action[] = [
    { id: 'clean', label: 'Clean up' },
    { id: 'fresh', label: s.freshArmed ? 'Click again to clear' : 'Start fresh' },
  ]
  const quality: Card = q
    ? {
        id: 'quality',
        title: `Quality ${gradeOf(q.score)} ${Math.round(q.score)}`,
        body: [plain(q.drag ? `Biggest drag: ${q.drag}.` : 'Nothing is dragging quality down.')],
        actions: qualityActions,
      }
    : { id: 'quality', title: 'Quality --', body: [plain('The score appears after the first tool call.')], actions: qualityActions }

  const context: Card =
    s.contextTokens != null && s.contextWindow != null
      ? { id: 'context', title: `${tokens(s.contextTokens)} of ${tokens(s.contextWindow)} tokens`, body: [plain('Quality holds best under half full.')], actions: [] }
      : { id: 'context', title: 'Context --', body: [plain('The fill appears after the first reply.')], actions: [] }

  const out = [quality, context, cacheCard(s.cache)]
  if (s.fiveHour) out.push(limitCard('fiveHour', s.fiveHour, s.now, opts))
  if (s.week) out.push(limitCard('week', s.week, s.now, opts))
  return out
}

function savingsBlock(s: Snapshot): SavingsBlock {
  const sv = s.savings
  if (!sv) {
    return {
      state: s.savingsLoading ? 'loading' : 'unavailable',
      sessionTokens: null,
      last30Tokens: null,
      sessionText: '--',
      last30Text: '--',
      daily: [],
      bars: [],
      reason: s.savingsLoading ? null : 'Savings appear once Token Optimizer has measured some.',
    }
  }
  const top = Math.max(1, ...sv.daily)
  return {
    state: s.savingsLoading ? 'loading' : 'ready',
    sessionTokens: sv.sessionTokens,
    last30Tokens: sv.last30Tokens,
    sessionText: sv.sessionTokens != null ? tokens(sv.sessionTokens) : '--',
    last30Text: sv.last30Tokens != null ? tokens(sv.last30Tokens) : '--',
    daily: sv.daily,
    bars: sv.daily.map((v) => Math.max(8, Math.round((Math.max(0, v) / top) * 100))),
    reason: null,
  }
}

/**
 * The detail row under Clawd (R6): only what the bar does not show.
 * `s.now` is epoch milliseconds; the quality cache's epochs are seconds.
 */
export function row(s: Snapshot, _opts: FormatOptions = {}): Row {
  const q = s.quality
  const facts: Fact[] = []
  if (s.branch) facts.push({ icon: 'branch', runs: [strong(s.branch)] })
  if (q?.sessionStartEpoch != null) facts.push({ icon: 'clock', runs: [strong(duration(s.now / 1000 - q.sessionStartEpoch))] })
  if (q?.toolCalls != null) facts.push({ icon: 'tool', runs: [strong(String(q.toolCalls)), plain(q.toolCalls === 1 ? ' tool call' : ' tool calls')] })
  if (q && q.compactions > 0) facts.push({ icon: 'compact', runs: [plain('Compacted '), strong(`${q.compactions}×`)] })
  facts.push({
    icon: 'bookmark',
    runs: (s.checkpointEpoch ?? q?.checkpointEpoch) != null
      ? [plain(`Checkpoint saved ${ago((s.checkpointEpoch ?? q!.checkpointEpoch!) * 1000, s.now)}`)]
      : s.earlierCheckpoint
        ? [plain(`Earlier checkpoint ${ago(s.earlierCheckpoint.epoch * 1000, s.now)}`), ...(s.earlierCheckpoint.about ? [plain(' · '), strong(s.earlierCheckpoint.about)] : [])]
        : [plain('No checkpoint yet')],
  })
  return { facts, savings: savingsBlock(s) }
}
