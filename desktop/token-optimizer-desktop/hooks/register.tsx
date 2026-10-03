// The Token Optimizer band above the prompt in the desktop app (U8, U9).
//
// This is the plugin's one hooks module and the only file that touches `$`.
// Every decision lives in ../src (pure, Node-tested) and ./data.ts (the data
// gatherer over a port). The engine allows `$` only into functions declared
// at the top of this file, so each `$` helper below is one, and closures made
// inside hooks only ever hand `$` on to them.
//
// State the drawing reads lives in `$.state` atoms (types/index.d.ts) so a hot
// reload keeps it; the pose reducer also runs from a module copy so streaming
// chunks do not write on every piece.
import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register } from 'claude-code'

import type { TokenOptimizerDesktopSession } from '../types/index.d.ts'
import {
  BUSY_TIMEOUT_MS,
  CAPTURE_TIMEOUT_MS,
  HANDOFF_KEY,
  RESUME_TIMEOUT_MS,
  WARM_PROMPT,
  attachesHandoff,
  busyNow,
  initialUi,
  isArmed,
  lifetimeFromUsage,
  noteNow,
  prepareHandoff,
  requestContextTokens,
  stripCrossSessionPointer,
  warmToast,
  withBusy,
  withNote,
  type Busy,
  type Handoff,
  type HandoffPort,
  type UiState,
} from '../src/actions.ts'
import { canKeepWarm, initialClock, reduceClock, view, type ClockEvent, type ClockState } from '../src/clock.ts'
import { DARK, LIGHT, clawdSvg, type Palette } from '../src/clawd.ts'
import type { Snapshot } from '../src/contracts.ts'
import { ICONS, ICON_ALT, iconSvg, type IconName } from '../src/icons.ts'
import { moodOf, sentence, type ActionId, type Run } from '../src/ladder.ts'
import { cards, marks, row, type Card, type Mark, type MarkTone } from '../src/marks.ts'
import { DEBOUNCE_MS, initialPose, reducePose, type PoseEvent, type PoseState } from '../src/pose.ts'
import {
  QUALITY_REFRESH_MS,
  STATUS_AFTER_TURN_MS,
  TICK_IDLE_MS,
  TICK_WARNING_MS,
  findTokenOptimizerRoot,
  gather,
  mergeStored,
  readHome,
  runMeasure,
  type DataIo,
  type GatherOptions,
} from './data.ts'

const sessionAtom = atom({ plugin: 'token-optimizer-desktop', key: 'session' } as const, null)
const handoffAtom = atom({ plugin: 'token-optimizer-desktop', key: 'handoff' } as const, null)
const clockAtom = atom({ plugin: 'token-optimizer-desktop', key: 'clock' } as const, null)
const poseAtom = atom({ plugin: 'token-optimizer-desktop', key: 'pose' } as const, null)
const uiAtom = atom({ plugin: 'token-optimizer-desktop', key: 'ui' } as const, null)
const themeAtom = atom({ plugin: 'token-optimizer-desktop', key: 'theme' } as const, 'light')
const frameAtom = atom({ plugin: 'token-optimizer-desktop', key: 'frame' } as const, 0)

type Desktop = Elements['desktop']
type Tones = { good: string; caution: string; bad: string; cold: string; ink: string; track: string; card: string; line: string }

/** The design page's tone colours, light and dark; bad, cold and ink come from Clawd's palette. */
function tonesFor(theme: 'light' | 'dark', p: Palette): Tones {
  return theme === 'dark'
    ? { good: '#57c27c', caution: '#e0a63c', bad: p.bad, cold: p.cold, ink: p.ink, track: 'rgba(243,241,234,0.17)', card: p.card, line: '#3e3e3b' }
    : { good: '#2f9e55', caution: '#c98a1b', bad: p.bad, cold: p.cold, ink: p.ink, track: 'rgba(31,30,29,0.13)', card: p.card, line: '#e2dfd6' }
}

// ---- module state: plain variables, rebuilt from the atoms after a reload ----

let enabled = true
let animate = true
let active = false
let timers: { cancel: () => void }[] = []
let statusTimer: { cancel: () => void } | null = null
let poseLive: PoseState | null = null
let poseSig = ''
let settleQueued = false
let lastCold = false
let frameText = ''
let frameAt = 0
let transcriptPath: string | null = null
let warmInFlight = false

const attempt = async <T,>(work: () => Promise<T>, fallback: T): Promise<T> => {
  try {
    return await work()
  } catch {
    return fallback
  }
}

/** Session ids become file names; keep what Token Optimizer's sanitizer keeps (as data.ts does). */
const cleanId = (id: string): string => id.replace(/[^a-zA-Z0-9_-]/g, '')

// ---- data ----

/** The gatherer's port over `$` (data.ts never sees `$`). */
function dataIo($: EngineInterface): DataIo {
  return {
    now: () => $.clock.now(),
    sessionId: () => $.session.id(),
    cwd: () => $.session.cwd(),
    envHome: () => $.env.get('HOME'),
    envUserProfile: () => $.env.get('USERPROFILE'),
    usage: () => $.session.usage(),
    list: path => $.fs.list(path),
    stat: path => $.fs.stat(path),
    read: async path => {
      const text = await $.fs.read(path)
      return typeof text === 'string' ? text : ''
    },
    run: (argv, init) => $.process.run(argv, init),
  }
}

/** Gather and store the session's figures, then let the cache clock learn from them (KTD12, KTD13). */
async function refresh($: EngineInterface, options: GatherOptions = {}): Promise<void> {
  const current = await attempt(() => read($, sessionAtom), null)
  const transcript = options.transcript ?? transcriptPath ?? undefined
  const fresh = await gather(dataIo($), current, transcript ? { ...options, transcript } : options)
  await update($, sessionAtom, cur => mergeStored(cur, fresh, options.reset))
  await syncClock($, fresh)
}

/** The status command's anchor and measured lifetime feed the clock when they are newer than what it holds. */
async function syncClock($: EngineInterface, s: TokenOptimizerDesktopSession): Promise<void> {
  await attempt(
    () =>
      update($, clockAtom, cur => {
        let c: ClockState = cur ?? initialClock()
        if (s.cacheLifetime && c.lifetime !== s.cacheLifetime) c = reduceClock(c, { type: 'lifetime-measured', lifetime: s.cacheLifetime })
        if (s.lastRequestEpoch !== null) {
          const at = s.lastRequestEpoch * 1000
          if (c.anchor === null || at > c.anchor) {
            c = reduceClock(c, { type: 'request-done', at, lifetime: s.cacheLifetime, contextTokens: s.contextTokens ?? c.contextTokens ?? 0 })
          }
        }
        if (c.contextTokens === null && s.contextTokens !== null) c = { ...c, contextTokens: s.contextTokens }
        return c
      }),
    undefined,
  )
}

async function feedClock($: EngineInterface, event: ClockEvent, now?: number): Promise<void> {
  await attempt(() => update($, clockAtom, cur => reduceClock(cur ?? initialClock(), event, now)), undefined)
}

/**
 * One pose event. The reducer runs on the module copy; the atom is written
 * only when something the drawing or a reload needs has changed.
 */
async function feedPose($: EngineInterface, event: PoseEvent): Promise<void> {
  try {
    const now = await $.clock.now()
    if (!poseLive) poseLive = (await read($, poseAtom)) ?? initialPose(now)
    poseLive = reducePose(poseLive, event, now)
    const s = poseLive
    const sig = JSON.stringify([s.pose, s.since, s.working, s.sub, s.agents, s.permissions, s.questions, s.compacting, s.cold, s.until])
    if (sig !== poseSig) {
      poseSig = sig
      await update($, poseAtom, () => s)
    }
    // A sub-pose waiting out its debounce settles on a tick of its own.
    if (s.rawSub !== s.sub && !settleQueued) {
      settleQueued = true
      $.clock.after(DEBOUNCE_MS, () => {
        settleQueued = false
        void feedPose($, { type: 'tick', now: 0 })
      })
    }
  } catch {
    // A pose that cannot be stored never breaks the event it rode on.
  }
}

/** Permission prompts and questions have no closing event of their own: close them on the next sign of life. */
async function closeAsks($: EngineInterface): Promise<void> {
  for (let i = poseLive?.permissions ?? 0; i > 0; i--) await feedPose($, { type: 'permission-closed' })
  for (let i = poseLive?.questions ?? 0; i > 0; i--) await feedPose($, { type: 'question-closed' })
}

async function refreshTheme($: EngineInterface): Promise<void> {
  const rows = await attempt(() => $.config.list(), [])
  const value = rows.find(r => r.key === 'theme')?.value
  const theme = typeof value === 'string' && value.toLowerCase().includes('dark') ? 'dark' : 'light'
  await attempt(() => update($, themeAtom, () => theme), undefined)
}

function planDefault(s: TokenOptimizerDesktopSession | null): 3600 | 300 {
  // Rate limits mean a Claude plan: an hour of cache; the API keeps five minutes (K3).
  return s && (s.fiveHour || s.week) ? 3600 : 300
}

/**
 * Once a second: keep Clawd's transients and naps moving, and redraw when
 * what the band shows changes; inside the cache warning window that is every
 * second, otherwise every 30 seconds (KTD13).
 */
async function tick($: EngineInterface): Promise<void> {
  try {
    const now = await $.clock.now()
    await feedPose($, { type: 'tick', now })
    const clock = (await read($, clockAtom)) ?? initialClock()
    const session = await read($, sessionAtom)
    const ui = (await read($, uiAtom)) ?? initialUi()
    const v = view(clock, now, planDefault(session))
    const cold = v.state === 'cold'
    if (cold !== lastCold) {
      lastCold = cold
      await feedPose($, { type: 'cache-cold-changed', cold })
    }
    const text = [v.state, v.state === 'warning' ? v.secondsLeft : '', busyNow(ui, now), noteNow(ui, now), isArmed(ui, now)].join('|')
    if (text !== frameText || now - frameAt >= TICK_IDLE_MS) {
      frameText = text
      frameAt = now
      await update($, frameAtom, n => (n ?? 0) + 1)
    }
  } catch {
    // The next tick tries again.
  }
}

function startCadence($: EngineInterface): void {
  for (const t of timers) t.cancel()
  timers = []
  try {
    timers.push($.clock.every(TICK_WARNING_MS, () => void tick($)))
    timers.push($.clock.every(QUALITY_REFRESH_MS, () => void attempt(() => refresh($), undefined)))
  } catch {
    // No timers: the band still redraws on events.
  }
}

/** Everything a desktop session needs once: theme, a pending hand-off from disk, figures, the cadence. */
async function start($: EngineInterface): Promise<void> {
  await refreshTheme($)
  const stored = await attempt(() => $.store.get(HANDOFF_KEY), undefined)
  if (isHandoff(stored)) await attempt(() => update($, handoffAtom, cur => cur ?? stored), undefined)
  await feedPose($, { type: 'session-start' })
  await attempt(() => refresh($, { savings: true }), undefined)
  startCadence($)
}

function isHandoff(v: unknown): v is Handoff {
  if (!v || typeof v !== 'object') return false
  const h = v as Record<string, unknown>
  return typeof h.fromSessionId === 'string' && typeof h.text === 'string' && h.text !== '' && typeof h.checkpointPath === 'string' && typeof h.createdAt === 'number'
}

// ---- buttons (U9) ----

function toast($: EngineInterface, text: string): void {
  try {
    $.ui.toast(text)
  } catch {
    // A toast that cannot show is not worth failing a press over.
  }
}

async function setUi($: EngineInterface, fn: (ui: UiState) => UiState): Promise<void> {
  await attempt(() => update($, uiAtom, cur => fn(cur ?? initialUi())), undefined)
}

async function disarm($: EngineInterface): Promise<void> {
  const ui = await attempt(() => read($, uiAtom), null)
  if (ui?.freshArmedAt != null) await setUi($, u => ({ ...u, freshArmedAt: null }))
}

const BUSY_WORDS: Record<Exclude<Busy, null>, string> = {
  clean: 'Clean up',
  'fresh-capture': 'Start fresh',
  'fresh-clear': 'Start fresh',
}

/** A busy state ends by itself (R13): a step label never outlives its work. */
function armBusyTimeout($: EngineInterface, busy: Exclude<Busy, null>, since: number): void {
  try {
    $.clock.after(BUSY_TIMEOUT_MS, () => void expireBusy($, busy, since))
  } catch {
    // busyNow() still treats it as over after the timeout.
  }
}

async function expireBusy($: EngineInterface, busy: Exclude<Busy, null>, since: number): Promise<void> {
  const ui = await attempt(() => read($, uiAtom), null)
  if (!ui || ui.busy !== busy || ui.busySince !== since) return
  const now = await $.clock.now()
  await setUi($, u => withNote(withBusy(u, null, now), `${BUSY_WORDS[busy]} timed out.`, now))
  if (busy === 'fresh-clear') {
    // The clear never came: the hand-off belongs to no new session.
    await dropHandoff($)
  }
  toast($, `${BUSY_WORDS[busy]} timed out.`)
}

async function dropHandoff($: EngineInterface): Promise<void> {
  await attempt(() => update($, handoffAtom, () => null), undefined)
  await attempt(() => $.store.delete(HANDOFF_KEY), undefined)
}

async function isTurnRunning($: EngineInterface): Promise<boolean> {
  const clock = await attempt(() => read($, clockAtom), null)
  return Boolean(clock?.working || poseLive?.working)
}

/** Clean up (KTD9): compaction with Token Optimizer's own PreCompact guidance, run outside the press. */
async function cleanUp($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
  if (busyNow(ui, now) !== null) return
  await disarm($)
  if (await isTurnRunning($)) {
    toast($, 'Clean up waits until the turn finishes.')
    return
  }
  await setUi($, u => withBusy(u, 'clean', now))
  armBusyTimeout($, 'clean', now)
  $.clock.after(0, () => void runCompact($, now))
}

async function runCompact($: EngineInterface, since: number): Promise<void> {
  let skip: string | null = null
  try {
    const result = await $.session.compact()
    if (result.skip) skip = result.skip
  } catch (error) {
    skip = error instanceof Error && error.message ? error.message.split('\n')[0] ?? 'it failed' : 'it failed'
  }
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile, or another step took over: nothing to report here.
  if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
  const now = await $.clock.now()
  if (skip !== null) {
    const line = `Clean up skipped: ${skip.replace(/\.$/, '')}.`
    await setUi($, u => withNote(withBusy(u, null, now), line, now))
    toast($, line)
  } else {
    await setUi($, u => withNote(withBusy(u, null, now), 'Cleaned up.', now))
  }
  $.clock.after(0, () => void attempt(() => refresh($), undefined))
}

/** Keep warm (KTD11, R12): guarded at press time, one fork, never on a timer. */
async function keepWarm($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  await disarm($)
  const clock = (await attempt(() => read($, clockAtom), null)) ?? initialClock()
  if (warmInFlight || clock.warming) {
    toast($, 'A warm-up is already running.')
    return
  }
  if (!canKeepWarm(clock, now)) {
    toast($, clock.working || (await isTurnRunning($)) ? 'Keep warm waits until the turn finishes.' : 'The cache is too close to dropping to keep it warm.')
    return
  }
  warmInFlight = true
  await feedClock($, { type: 'warm-start' }, now)
  $.clock.after(0, () => void runWarm($, clock.contextTokens))
}

async function runWarm($: EngineInterface, known: number | null): Promise<void> {
  try {
    const session = await attempt(() => read($, sessionAtom), null)
    const contextTokens = known ?? session?.contextTokens ?? 0
    const reply = await $.model.fork({ prompt: WARM_PROMPT })
    const at = await $.clock.now()
    if (reply.isAnswered) {
      await feedClock($, { type: 'warm-done', at, cacheReadTokens: reply.usage.cache_read_input_tokens, contextTokens })
      const after = await attempt(() => read($, clockAtom), null)
      toast($, warmToast({ ok: true, lapsed: Boolean(after?.lapsed), contextTokens }))
    } else {
      await feedClock($, { type: 'warm-failed' })
      toast($, warmToast({ ok: false, reason: reply.reason }))
    }
  } catch {
    await feedClock($, { type: 'warm-failed' })
    toast($, warmToast({ ok: false, reason: 'the request failed' }))
  } finally {
    warmInFlight = false
  }
}

/** Start fresh (KTD10, R14): first press arms, a second within 5 s runs it. */
async function startFresh($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
  if (busyNow(ui, now) !== null) return
  if (!isArmed(ui, now)) {
    await setUi($, u => ({ ...u, freshArmedAt: now }))
    return
  }
  if (await isTurnRunning($)) {
    await disarm($)
    toast($, 'Start fresh waits until the turn finishes.')
    return
  }
  const sid = cleanId(await attempt(() => $.session.id(), ''))
  if (!sid) {
    await disarm($)
    toast($, 'Start fresh stopped: no session to save. Nothing was cleared.')
    return
  }
  await setUi($, u => withBusy(u, 'fresh-capture', now))
  armBusyTimeout($, 'fresh-capture', now)
  $.clock.after(0, () => void runFresh($, sid, now))
}

async function runFresh($: EngineInterface, sid: string, since: number): Promise<void> {
  const io = dataIo($)
  const stop = async (reason: string): Promise<void> => {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busySince !== since) return
    const now = await $.clock.now()
    const line = `Start fresh stopped: ${reason}. Nothing was cleared.`
    await setUi($, u => withNote(withBusy(u, null, now), line, now))
    toast($, line)
  }

  const root = await findTokenOptimizerRoot(io, await readHome(io))
  if (!root) return stop('Token Optimizer was not found')
  const port: HandoffPort = {
    run: (args, stdin) =>
      runMeasure(io, root, args, {
        timeoutMs: args[0] === 'compact-capture' ? CAPTURE_TIMEOUT_MS : RESUME_TIMEOUT_MS,
        ...(stdin === undefined ? {} : { stdin }),
      }),
    read: path => io.read(path),
  }
  const result = await prepareHandoff(port, { sessionId: sid, transcriptPath, now: since })
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile: the person was told; clear nothing.
  if (!ui || ui.busy !== 'fresh-capture' || ui.busySince !== since) return
  if (!result.ok) return stop(result.reason)

  try {
    await $.store.set(HANDOFF_KEY, result.handoff)
  } catch {
    return stop('the hand-off could not be kept on disk')
  }
  await attempt(() => update($, handoffAtom, () => result.handoff), undefined)
  const now = await $.clock.now()
  await setUi($, u => withBusy(u, 'fresh-clear', now))
  armBusyTimeout($, 'fresh-clear', now)
  $.clock.after(0, () => void runClear($, now))
}

async function runClear($: EngineInterface, since: number): Promise<void> {
  try {
    await $.command.run({ command: 'clear' })
  } catch {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busy !== 'fresh-clear' || ui.busySince !== since) return
    const now = await $.clock.now()
    await dropHandoff($)
    await setUi($, u => withNote(withBusy(u, null, now), 'Start fresh could not clear. Your conversation is unchanged.', now))
    toast($, 'Start fresh could not clear. Your conversation is unchanged.')
  }
}

async function toggleDetails($: EngineInterface): Promise<void> {
  await disarm($)
  const current = await attempt(() => read($, sessionAtom), null)
  if (!current) return
  const opening = !current.sheetOpen
  await update($, sessionAtom, cur => (cur ? { ...cur, sheetOpen: !cur.sheetOpen } : cur))
  // Savings read when the row opens (R17).
  if (opening) $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
}

async function act($: EngineInterface, id: ActionId): Promise<void> {
  try {
    if (id === 'clean' || id === 'clean-first') await cleanUp($)
    else if (id === 'fresh') await startFresh($)
    else await keepWarm($)
  } catch {
    toast($, 'That did not work. Try again in a moment.')
  }
}

// ---- drawing (U8) ----

const ring = (p: number, color: string, track: string, cold: boolean): string => {
  const c = 2 * Math.PI * 7
  const bg = cold ? `stroke="${color}" stroke-dasharray="2.2 2.2"` : `stroke="${track}"`
  return (
    `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" ${bg}/>` +
    (p > 0 ? `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" stroke="${color}" stroke-linecap="round" stroke-dasharray="${((c * p) / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 10 10)"/>` : '')
  )
}

const esc = (v: string): string => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** A mark's icon beside its ring or grade badge, as one small picture. */
function markSvg(mark: Mark, t: Tones): string {
  const color = toneColor(mark.tone, t)
  const icon = `<g transform="translate(0 2)" fill="none" stroke="${mark.tone === 'none' ? t.ink : color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[mark.icon]}</g>`
  const right =
    mark.badge !== undefined
      ? `<rect x="20" y="1" width="18" height="18" rx="5" fill="${mark.tone === 'none' ? t.track : color}"/>` +
        `<text x="29" y="14" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11.5" font-weight="700" fill="${t.card}">${esc(mark.badge)}</text>`
      : `<g transform="translate(19 0)">${ring(mark.ringPercent ?? 0, color, t.track, mark.tone === 'cold')}</g>`
  return `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20" role="img" aria-label="${esc(mark.alt)}"><title>${esc(mark.alt)}</title>${icon}${right}</svg>`
}

function toneColor(tone: MarkTone, t: Tones): string {
  return tone === 'none' ? t.track : t[tone]
}

/** The savings bars as one small picture: today in the good colour, the rest on the track. */
function barsSvg(bars: number[], t: Tones): { source: string; alt: string; width: number } {
  const width = bars.length * 6
  const alt = `Tokens saved by Token Optimizer on each of the past ${bars.length} days`
  const body = bars
    .map((h, i) => {
      const height = Math.max(2, Math.round((h / 100) * 28))
      return `<rect x="${i * 6}" y="${28 - height}" width="4" height="${height}" rx="1.5" fill="${i === bars.length - 1 ? t.good : t.track}"/>`
    })
    .join('')
  return { source: `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="28" viewBox="0 0 ${width} 28" role="img" aria-label="${alt}"><title>${alt}</title>${body}</svg>`, alt, width }
}

function chevronSvg(open: boolean, ink: string): { source: string; alt: string } {
  const alt = open ? 'Session details open' : 'Session details folded'
  const svg = iconSvg('chevron', ink, { alt })
  return { source: open ? svg.replace(ICONS.chevron, `<g transform="rotate(180 8 8)">${ICONS.chevron}</g>`) : svg, alt }
}

function runsOf(D: Desktop, runs: Run[], t: Tones) {
  const { Text } = D
  return runs.map(r => (r.lose ? <Text bold color={t.bad}>{r.text}</Text> : r.strong ? <Text bold>{r.text}</Text> : r.text))
}

function icon(D: Desktop, name: IconName, color: string, alt?: string) {
  const { Svg } = D
  const label = alt ?? ICON_ALT[name]
  return <Svg source={iconSvg(name, color, { alt: label })} alt={label} width={16} height={16} />
}

type Model = {
  snap: Snapshot
  palette: Palette
  tones: Tones
  pose: PoseState['pose']
  sheetOpen: boolean
  savingsReason: string | null
  narrow: boolean
}

type Handlers = { act: (id: ActionId) => void; details: () => void }

function cardBox(D: Desktop, card: Card, index: number, t: Tones, on: Handlers) {
  const { Box, Text, Button } = D
  const side = index < 2 ? { left: 0 } : { right: 0 }
  return (
    <Box
      position="absolute"
      bottom={1}
      {...side}
      display="none"
      hover={{ display: 'flex' }}
      flexDirection="column"
      width={34}
      paddingX={1}
      borderStyle="round"
      borderColor={t.line}
      backgroundColor={t.card}
    >
      <Text bold>{card.title}</Text>
      <Text wrap="wrap">{runsOf(D, card.body, t)}</Text>
      {card.actions.length > 0 ? (
        <Box flexDirection="row" columnGap={1} marginTop={1}>
          {card.actions.map(a => (
            <Button key={`card-${card.id}-${a.id}`} label={a.label} onPress={() => on.act(a.id)} />
          ))}
        </Box>
      ) : (
        ''
      )}
    </Box>
  )
}

function drawBand(D: Desktop, m: Model, on: Handlers) {
  const { Box, Text, Button, Svg } = D
  const { snap, tones: t } = m
  const say = sentence(snap)
  const markList = marks(snap)
  const cardList = cards(snap)
  const detail = row(snap)
  const chevron = chevronSvg(m.sheetOpen, t.ink)
  const clawd = clawdSvg(m.pose, moodOf(snap), { animate, palette: m.palette })
  const clawdAlt = /aria-label="([^"]*)"/.exec(clawd)?.[1] ?? 'Clawd'
  const bars = m.narrow ? detail.savings.bars.slice(-14) : detail.savings.bars

  return (
    <Box flexDirection="row" alignItems="flex-start" columnGap={2} paddingX={1}>
      <Box flexDirection="column" alignItems="center" flexShrink={0}>
        <Svg source={clawd} alt={clawdAlt} width={72} height={57} isInteractive />
        <Box flexDirection="row" alignItems="center" columnGap={0}>
          <Svg source={chevron.source} alt={chevron.alt} width={16} height={16} />
          <Button key="details" plain label={m.sheetOpen ? 'Hide session details' : 'Show session details'} onPress={() => on.details()} />
        </Box>
      </Box>
      <Box flexDirection="column" flexGrow={1} flexShrink={1} rowGap={1}>
        <Box flexDirection="row" alignItems="flex-start" justifyContent="space-between" columnGap={2}>
          <Box flexDirection="row" alignItems="flex-start" columnGap={1} flexShrink={1}>
            <Text bold>Token Optimizer</Text>
            {icon(D, say.icon, toneColor(say.tone, t))}
            <Text wrap="wrap">{runsOf(D, say.runs, t)}</Text>
          </Box>
          {say.action ? <Button key="action" variant="primary" label={say.action.label} onPress={() => on.act(say.action!.id)} /> : ''}
        </Box>
        <Box flexDirection="row" flexWrap="wrap" columnGap={3}>
          {markList.map((mark, i) => (
            <Box key={`mark-${mark.id}`} position="relative" flexDirection="row" alignItems="center" columnGap={1}>
              <Svg source={markSvg(mark, t)} alt={mark.alt} width={40} height={20} />
              <Text bold>{mark.value}</Text>
              <Text>{mark.label}</Text>
              {cardList[i] ? cardBox(D, cardList[i], i, t, on) : ''}
            </Box>
          ))}
        </Box>
        {m.sheetOpen ? (
          <Box key="row" flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={3} rowGap={1}>
            {detail.facts.map(f => (
              <Box flexDirection="row" alignItems="center" columnGap={1}>
                {icon(D, f.icon, t.ink)}
                <Text>{runsOf(D, f.runs, t)}</Text>
              </Box>
            ))}
            <Box flexDirection="row" alignItems="center" columnGap={1}>
              {bars.length > 0 ? (() => {
                const b = barsSvg(bars, t)
                return <Svg source={b.source} alt={b.alt} width={b.width} height={28} />
              })() : icon(D, 'bookmark', t.ink, 'Savings')}
              <Box flexDirection="column">
                <Text>
                  <Text bold>{detail.savings.sessionText}</Text> saved this session
                </Text>
                <Text>
                  <Text bold>{detail.savings.last30Text}</Text> past 30 days
                </Text>
                {detail.savings.state === 'unavailable' ? <Text>{m.savingsReason ?? detail.savings.reason ?? ''}</Text> : ''}
              </Box>
            </Box>
          </Box>
        ) : (
          ''
        )}
      </Box>
    </Box>
  )
}

// ---- hooks ----

export const register: Register = (on, options) => {
  enabled = options.enabled !== false
  animate = options.animate !== false

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    active = enabled && e.isInteractive && e.surface !== 'terminal' && e.surface !== 'vscode'
    if (active) await start($)
    return result
  })

  // A clear: no session.start follows; this start carries the new id (KTD12, R15).
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    if (!active || e.source !== 'clear') return result
    if (e.transcript_path) transcriptPath = e.transcript_path
    const sid = cleanId(e.session_id)
    await feedClock($, { type: 'clear' })
    lastCold = false
    await feedPose($, { type: 'session-start' })
    const handoff = await attempt(() => read($, handoffAtom), null)
    const now = await $.clock.now()
    await setUi($, u => (u.busy === 'fresh-clear' ? withBusy(u, null, now) : u))
    await attempt(() => refresh($, { sessionId: e.session_id, reset: true, savings: true, transcript: e.transcript_path || undefined }), undefined)
    // The held hand-off replaces Token Optimizer's cross-session pointer (KTD10).
    if (handoff && handoff.fromSessionId !== sid && result.additionalContext) {
      return { ...result, additionalContext: stripCrossSessionPointer(result.additionalContext) ?? [] }
    }
    return result
  })

  // The hand-off joins the first prompt the person sends, once (KTD10).
  on('prompt.submit', async ($, e, next) => {
    if (!active) return next(e)
    const handoff = await attempt(() => read($, handoffAtom), null)
    if (!handoff || !attachesHandoff(e.origin?.kind)) return next(e)
    const sid = cleanId(await attempt(() => $.session.id(), ''))
    if (!sid || sid === handoff.fromSessionId) return next(e)
    await dropHandoff($)
    return next({ ...e, context: [...(e.context ?? []), handoff.text] })
  })

  // compact-capture wants the transcript; these classic events carry its path.
  on('classic.UserPromptSubmit', ($, e, next) => {
    if (e.transcript_path) transcriptPath = e.transcript_path
    return next(e)
  })
  on('classic.Stop', ($, e, next) => {
    if (e.transcript_path) transcriptPath = e.transcript_path
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    if (active) {
      await feedClock($, { type: 'working-changed', working: true })
      await feedPose($, { type: 'turn-start' })
      await feedPose($, { type: 'working-changed', working: true })
    }
    return next(e)
  })

  // Per-request usage lives on each step's stop chunk; turn.complete's is summed (U1).
  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (!active || e.agentId !== undefined) return yield* stream
    let last: string | null = null
    for (;;) {
      const step = await stream.next()
      if (step.done) return step.value
      const chunk = step.value
      try {
        if ((chunk.kind === 'thinking' || chunk.kind === 'text') && chunk.kind !== last) {
          last = chunk.kind
          await feedPose($, { type: chunk.kind === 'thinking' ? 'thinking' : 'text' })
        } else if (chunk.kind === 'tool') {
          last = 'tool'
        } else if (chunk.kind === 'stop' && chunk.usage) {
          const at = await $.clock.now()
          await feedClock($, { type: 'request-done', at, lifetime: lifetimeFromUsage(chunk.usage), contextTokens: requestContextTokens(chunk.usage) })
        }
      } catch {
        // Watching the stream never breaks it.
      }
      yield chunk
    }
  })

  on('tool.call', async ($, e, next) => {
    if (!active) return next(e)
    const tool = String(e.tool)
    const agentId = e.agentId
    await closeAsks($)
    await feedPose($, agentId === undefined ? { type: 'tool-call', tool } : { type: 'tool-call', tool, agentId })
    // The question dialog is open from the call until it answers.
    const asking = tool === 'AskUserQuestion' && agentId === undefined
    if (asking) await feedPose($, { type: 'question-open' })
    try {
      return await next(e)
    } finally {
      if (asking) await feedPose($, { type: 'question-closed' })
      await closeAsks($)
      await feedPose($, agentId === undefined ? { type: 'tool-done' } : { type: 'tool-done', agentId })
    }
  })

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    if (active && result.agentId) await feedPose($, { type: 'agent-spawn', agentId: result.agentId, background: e.background })
    return result
  })

  on('classic.PermissionRequest', async ($, e, next) => {
    if (active) await feedPose($, { type: 'permission-open' })
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    if (!active || e.trigger === 'precompute' || e.agentId !== undefined) return next(e)
    await feedPose($, { type: 'compact-start' })
    try {
      return await next(e)
    } finally {
      await feedPose($, { type: 'compact-end' })
      $.clock.after(0, () => void attempt(() => refresh($), undefined))
    }
  })

  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (!active) return result
    if (e.agentId !== undefined) {
      await feedPose($, { type: 'turn-complete', reason: e.reason, agentId: e.agentId })
      return result
    }
    await closeAsks($)
    await feedClock($, { type: 'working-changed', working: false })
    await feedPose($, { type: 'turn-complete', reason: e.reason })
    await feedPose($, { type: 'working-changed', working: false })
    // Quality after each turn now; savings and the clock facts a little later (KTD13).
    $.clock.after(0, () => void attempt(() => refresh($), undefined))
    statusTimer?.cancel()
    statusTimer = $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(() => refresh($, { savings: true }), undefined))
    return result
  })

  on('config.set', async ($, e, next) => {
    const result = await next(e)
    if (active && e.key === 'theme') $.clock.after(0, () => void refreshTheme($))
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // The terminal keeps its own status line (K6); a survey holds the band.
    if (!enabled || e.surface !== 'desktop' || e.props.hasSurvey) return next(e)
    if (!active) {
      // A desktop band without a session.start of ours (a late enable): start now, outside the drawing.
      active = true
      $.clock.after(0, () => void start($))
    }

    await read($, frameAtom)
    const now = await $.clock.now()
    const stored = await read($, sessionAtom)
    const clock = (await read($, clockAtom)) ?? initialClock()
    const ui = (await read($, uiAtom)) ?? initialUi()
    const pose = await read($, poseAtom)
    const theme = await read($, themeAtom)
    const handoff = await read($, handoffAtom)
    const liveSid = cleanId(await attempt(() => $.session.id(), ''))
    // A stored figure of another session never shows (R15).
    const s = stored && (!liveSid || stored.sessionId === liveSid) ? stored : null
    const working = e.props.isWorking

    const snap: Snapshot = {
      now,
      working,
      quality: s?.quality ?? null,
      contextPercent: s?.contextPercent ?? null,
      contextTokens: s?.contextTokens ?? null,
      contextWindow: s?.contextWindow ?? null,
      fiveHour: s?.fiveHour ?? null,
      week: s?.week ?? null,
      cache: view({ ...clock, working }, now, planDefault(s)),
      branch: s?.branch ?? null,
      savings: s?.savings ?? null,
      savingsLoading: s?.savingsState === 'loading',
      busy: busyNow(ui, now) ?? (clock.warming ? 'warming' : null),
      note: noteNow(ui, now),
      handoffPending: handoff !== null && handoff.fromSessionId !== liveSid,
      freshArmed: isArmed(ui, now),
    }
    const palette = theme === 'dark' ? DARK : LIGHT

    return drawBand(
      $.ui.resolve(e),
      {
        snap,
        palette,
        tones: tonesFor(theme === 'dark' ? 'dark' : 'light', palette),
        pose: pose?.pose ?? 'idle',
        sheetOpen: s?.sheetOpen ?? false,
        savingsReason: s?.savingsReason ?? null,
        narrow: e.props.bodyColumns < 90,
      },
      {
        act: id => void act($, id),
        details: () => void toggleDetails($),
      },
    )
  })
}
