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
  HANDOFF_TTL_MS,
  RESUME_TIMEOUT_MS,
  WARM_PROMPT,
  attachesHandoff,
  busyNow,
  handoffFate,
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
import { LIGHT, clawdSvg, type Gaze, type Palette } from '../src/clawd.ts'
import type { Snapshot } from '../src/contracts.ts'
import { ICONS, ICON_ALT, iconSvg, type IconName } from '../src/icons.ts'
import { COMPACT_HEAVY, QUALITY_FLOOR, moodOf, sentence, type ActionId, type Run } from '../src/ladder.ts'
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
const engineCallAtom = atom({ plugin: 'token-optimizer-desktop', key: 'engineCall' } as const, null)
const uiAtom = atom({ plugin: 'token-optimizer-desktop', key: 'ui' } as const, null)
const themeAtom = atom({ plugin: 'token-optimizer-desktop', key: 'theme' } as const, 'light')
const frameAtom = atom({ plugin: 'token-optimizer-desktop', key: 'frame' } as const, 0)

type Desktop = Elements['desktop']
type Tones = { good: string; caution: string; bad: string; cold: string; ink: string; track: string; card: string; line: string }

/** The design page's tone colours, light and dark; bad, cold and ink come from Clawd's palette. */
function tonesFor(theme: 'light' | 'dark', p: Palette): Tones {
  return theme === 'dark'
    ? { good: '#57c27c', caution: '#e0a63c', bad: p.bad, cold: p.cold, ink: p.ink, track: '#4b4a46', card: p.card, line: '#3e3e3b' }
    : { good: '#2f9e55', caution: '#c98a1b', bad: p.bad, cold: p.cold, ink: p.ink, track: '#d9d6cd', card: p.card, line: '#e2dfd6' }
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
/** The cache coldness last told to Clawd; null until the first tick tells him, so a cold pose kept across a reload never sticks. */
let lastCold: boolean | null = null
let frameText = ''
let transcriptPath: string | null = null
let warmInFlight = false
/**
 * Which session the band is serving: bumped when one starts, is cleared or is
 * swapped in. Work begun under one generation drops its result in the next.
 */
let sessionGen = 0
/**
 * A press is being claimed: a second press meanwhile is ignored (no double
 * compaction). A claim older than the busy timeout is stale: a stuck press
 * never holds the buttons past it (R3).
 */
let claim: { at: number } | null = null
/**
 * The one engine call the buttons may have running: our `$.session.compact()`
 * or our `$.command.run({ command: 'clear' })`. Set before the call's first
 * await, cleared only when it settles, whatever the busy timeout says: a
 * second compaction or clear never overlaps it. `engineCallAtom` records it
 * too, so a reload mid-call still refuses (see runningCall).
 */
let engineCall: 'compact' | 'clear' | null = null
/** The live session a render last asked to re-read after finding another session's figures (TR-04). */
let resyncFor = ''

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
    pluginRoot: () => $.plugin.root,
  }
}

/** Gather and store the session's figures, then let the cache clock learn from them (KTD12, KTD13). */
async function refresh($: EngineInterface, options: GatherOptions = {}): Promise<void> {
  const gen = sessionGen
  const current = await attempt(() => read($, sessionAtom), null)
  const transcript = options.transcript ?? transcriptPath ?? undefined
  const fresh = await gather(dataIo($), current, transcript ? { ...options, transcript } : options)
  // Begun before a clear or a session change: its figures belong to the old session.
  if (gen !== sessionGen) return
  if (!options.reset && current !== null && fresh.sessionId !== '' && current.sessionId !== fresh.sessionId) {
    // Another session (a new one, or a resume): nothing of the last one carries over (R15, KTD12).
    sessionGen += 1
    await feedClock($, { type: 'clear' })
    await setUi($, () => initialUi())
    lastCold = null
    await feedPose($, { type: 'session-start' })
  }
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
  // After a reload the module copy is empty until the first pose event: read the stored one.
  if (!poseLive) poseLive = await attempt(() => read($, poseAtom), null)
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
    // Only a change the band shows redraws it: each redraw risks restarting Clawd's animation.
    const shown = v.state === 'warning' ? v.secondsLeft : v.secondsLeft != null ? Math.ceil(v.secondsLeft / 60) : ''
    const text = [v.state, shown, Math.floor(now / TICK_IDLE_MS), busyNow(ui, now), noteNow(ui, now), isArmed(ui, now)].join('|')
    if (text !== frameText) {
      frameText = text
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
  sessionGen += 1
  // A warm-up marked running with no fork in flight here was cut off by a reload (TR-10).
  const clock = await attempt(() => read($, clockAtom), null)
  if (clock?.warming && !warmInFlight) await feedClock($, { type: 'warm-failed' })
  await refreshTheme($)
  const held = await heldHandoff($)
  if (held) await handoffThatFits($, held)
  await feedPose($, { type: 'session-start' })
  await attempt(() => refresh($, { savings: true }), undefined)
  startCadence($)
}

function isHandoff(v: unknown): v is Handoff {
  if (!v || typeof v !== 'object') return false
  const h = v as Record<string, unknown>
  return typeof h.fromSessionId === 'string' && typeof h.cwd === 'string' && typeof h.text === 'string' && h.text !== '' && typeof h.checkpointPath === 'string' && typeof h.createdAt === 'number'
}

/**
 * The pending hand-off. `$.store` is the truth (it survives restarts and is
 * deleted when one session takes it); the atom only mirrors it for drawing. A
 * store that cannot be read holds nothing: no stale mirror is ever attached.
 */
async function heldHandoff($: EngineInterface): Promise<Handoff | null> {
  let stored: unknown
  try {
    stored = await $.store.get(HANDOFF_KEY)
  } catch {
    return null
  }
  const held = isHandoff(stored) ? stored : null
  if (stored != null && !held) await attempt(() => $.store.delete(HANDOFF_KEY), undefined)
  const mirror = await attempt(() => read($, handoffAtom), null)
  if (JSON.stringify(mirror) !== JSON.stringify(held)) await attempt(() => update($, handoffAtom, () => held), undefined)
  return held
}

/** When the live session began (`$.clock.now()` ms), or null when the engine cannot say. */
async function sessionStartedAt($: EngineInterface): Promise<number | null> {
  const usage = await attempt(() => $.session.usage(), null)
  return typeof usage?.startedAt === 'number' && Number.isFinite(usage.startedAt) ? usage.startedAt : null
}

/**
 * The held hand-off when it joins this session (KTD10): another session than
 * the one that saved it, in its project, started since the save, within 10
 * minutes of it. An expired one in this project is dropped with a one-line
 * note; another project's is left alone (R1). `as` names the session when the
 * caller knows its id better than the engine does yet (a clear's own start event).
 */
async function handoffThatFits($: EngineInterface, h: Handoff, as?: { sessionId: string; startedAt: number | null }): Promise<Handoff | null> {
  const sessionId = as?.sessionId ?? cleanId(await attempt(() => $.session.id(), ''))
  const cwd = await attempt(() => $.session.cwd(), '')
  const now = await $.clock.now()
  const startedAt = as ? as.startedAt : await sessionStartedAt($)
  const fate = handoffFate(h, { sessionId, cwd, startedAt, now })
  if (fate === 'attach') return h
  if (fate === 'skip') return null
  await dropHandoff($)
  const line = `Start fresh's saved hand-off was discarded: ${fate.drop}.`
  await setUi($, u => withNote(u, line, now))
  toast($, line)
  return null
}

/** The refusal for a press while our compaction or clear still runs. */
function stillRunning(kind: 'compact' | 'clear'): string {
  return kind === 'compact' ? 'Still finishing the last clean-up.' : 'Still clearing.'
}

/**
 * Our compaction or clear still running: this module's own, or one recorded
 * before a reload that is under 10 minutes old (an older record is cleared).
 */
async function runningCall($: EngineInterface): Promise<'compact' | 'clear' | null> {
  if (engineCall) return engineCall
  const held = await attempt(() => read($, engineCallAtom), null)
  if (engineCall) return engineCall
  if (!held) return null
  // A clock that cannot be read keeps the record: refuse rather than overlap.
  const now = await attempt(() => $.clock.now(), null)
  if (now === null || now - held.startedAt < HANDOFF_TTL_MS) return held.kind
  await attempt(() => update($, engineCallAtom, cur => (cur && cur.startedAt === held.startedAt && cur.kind === held.kind ? null : cur)), undefined)
  return null
}

/**
 * Claims the one engine call (the caller checked runningCall with no await
 * since) and records it for a reload. False when it cannot be recorded: the
 * call is not made.
 */
async function claimCall($: EngineInterface, kind: 'compact' | 'clear'): Promise<{ kind: 'compact' | 'clear'; startedAt: number } | null> {
  engineCall = kind
  try {
    const mine = { kind, startedAt: await $.clock.now() }
    await update($, engineCallAtom, () => mine)
    return mine
  } catch {
    engineCall = null
    return null
  }
}

/** The call settled: release it here and in the record (only our own record). */
async function releaseCall($: EngineInterface, mine: { kind: 'compact' | 'clear'; startedAt: number }): Promise<void> {
  engineCall = null
  await attempt(() => update($, engineCallAtom, cur => (cur && cur.kind === mine.kind && cur.startedAt === mine.startedAt ? null : cur)), undefined)
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
  // The clear waits for the turn to end and can still land: the hand-off stays
  // for the conversation it creates, within 10 minutes of the save (TR-06).
  const line = busy === 'fresh-clear' ? 'Start fresh clears when the current turn ends.' : `${BUSY_WORDS[busy]} timed out.`
  await setUi($, u => withNote(withBusy(u, null, now), line, now))
  toast($, line)
}

/** Deletes the held hand-off, store first; true only when the delete succeeded (the mirror follows it). */
async function dropHandoff($: EngineInterface): Promise<boolean> {
  try {
    await $.store.delete(HANDOFF_KEY)
  } catch {
    return false
  }
  await attempt(() => update($, handoffAtom, () => null), undefined)
  return true
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
  const running = await runningCall($)
  if (running) {
    toast($, stillRunning(running))
    return
  }
  if (await isTurnRunning($)) {
    toast($, 'Clean up waits until the turn finishes.')
    return
  }
  await setUi($, u => withBusy(u, 'clean', now))
  armBusyTimeout($, 'clean', now)
  $.clock.after(0, () => void runCompact($, now))
}

async function runCompact($: EngineInterface, since: number): Promise<void> {
  // A turn that started since the press would be cut short.
  if (await isTurnRunning($)) {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
    const now = await $.clock.now()
    await setUi($, u => withBusy(u, null, now))
    toast($, 'Clean up waits until the turn finishes.')
    return
  }
  const running = await runningCall($)
  if (running || engineCall) {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
    const now = await $.clock.now()
    await setUi($, u => withBusy(u, null, now))
    toast($, stillRunning(running ?? engineCall ?? 'compact'))
    return
  }
  let skip: string | null = null
  // Claimed with no await between the check and the claim; recorded before the call.
  const mine = await claimCall($, 'compact')
  if (!mine) {
    skip = 'it could not be recorded'
  } else {
    try {
      // The command, as if typed: $.session.compact() is refused in a headless
      // session, and the desktop app runs its sessions headless.
      await $.command.run({ command: 'compact' })
    } catch (error) {
      skip = error instanceof Error && error.message ? error.message.split('\n')[0] ?? 'it failed' : 'it failed'
    } finally {
      await releaseCall($, mine)
    }
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
  const gen = sessionGen
  await feedClock($, { type: 'warm-start' }, now)
  $.clock.after(0, () => void runWarm($, clock.contextTokens, gen))
}

type ForkReply = Awaited<ReturnType<EngineInterface['model']['fork']>>

/** The warm-up fork, or null once `ms` pass without an answer; a late answer is then ignored (TR-10). */
function forkWithin($: EngineInterface, ms: number): Promise<ForkReply | null> {
  return new Promise<ForkReply | null>((resolve, reject) => {
    let timer: { cancel: () => void } | null = null
    try {
      timer = $.clock.after(ms, () => resolve(null))
    } catch {
      // No timer: the fork alone decides.
    }
    $.model.fork({ prompt: WARM_PROMPT }).then(
      reply => {
        timer?.cancel()
        resolve(reply)
      },
      error => {
        timer?.cancel()
        reject(error)
      },
    )
  })
}

async function runWarm($: EngineInterface, known: number | null, gen: number): Promise<void> {
  try {
    const session = await attempt(() => read($, sessionAtom), null)
    // A recorded 0 is no size at all: fall back to the session's own (TR-21).
    const contextTokens = known || session?.contextTokens || 0
    const reply = await forkWithin($, BUSY_TIMEOUT_MS)
    // Cleared meanwhile: this warm-up says nothing about the new session (TR-09).
    if (gen !== sessionGen) return
    const at = await $.clock.now()
    if (reply === null) {
      await feedClock($, { type: 'warm-failed' })
      toast($, warmToast({ ok: false, reason: 'the request failed' }))
    } else if (reply.isAnswered) {
      await feedClock($, { type: 'warm-done', at, cacheReadTokens: reply.usage.cache_read_input_tokens, contextTokens })
      const after = await attempt(() => read($, clockAtom), null)
      toast($, warmToast({ ok: true, lapsed: Boolean(after?.lapsed), contextTokens }))
    } else {
      await feedClock($, { type: 'warm-failed' })
      toast($, warmToast({ ok: false, reason: reply.reason }))
    }
  } catch {
    if (gen !== sessionGen) return
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
  const running = await runningCall($)
  if (running) {
    await disarm($)
    toast($, stillRunning(running))
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
  const gen = sessionGen
  await setUi($, u => withBusy(u, 'fresh-capture', now))
  armBusyTimeout($, 'fresh-capture', now)
  $.clock.after(0, () => void runFresh($, sid, now, gen))
}

/** Still the session Start fresh was pressed in (TR-01): a typed /clear meanwhile means stand down. */
async function stillSession($: EngineInterface, sid: string, gen: number): Promise<boolean> {
  return gen === sessionGen && cleanId(await attempt(() => $.session.id(), '')) === sid
}

async function runFresh($: EngineInterface, sid: string, since: number, gen: number): Promise<void> {
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
  const cwd = await attempt(() => $.session.cwd(), '')
  // `now` is a placeholder: the hand-off is stamped once its save lands (below).
  const result = await prepareHandoff(port, { sessionId: sid, transcriptPath, cwd, now: since })
  const standDown = async (): Promise<void> => {
    const now = await $.clock.now()
    await setUi($, u => (u.busySince === since ? withBusy(u, null, now) : u))
  }
  // The session changed under it (a typed /clear, a resume): clear nothing, and say so (R2).
  const changed = async (): Promise<void> => {
    await standDown()
    toast($, 'Start fresh stopped: the session changed.')
  }
  if (!(await stillSession($, sid, gen))) return changed()
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile: the person was told; clear nothing.
  if (!ui || ui.busy !== 'fresh-capture' || ui.busySince !== since) return
  if (!result.ok) return stop(result.reason)
  // Our compaction or clear still running: never overlap it.
  const running = await runningCall($)
  if (running) return stop(running === 'compact' ? 'the last clean-up is still running' : 'the last clear is still running')
  // A turn started during the capture: a queued clear would wipe it (TR-02).
  if (await isTurnRunning($)) {
    await standDown()
    toast($, 'Start fresh waits until the turn finishes.')
    return
  }

  // Saved, then stamped from the clock right after the save lands: the
  // 10-minute window and "started since the save" both count from there.
  let handoff: Handoff
  try {
    await $.store.set(HANDOFF_KEY, result.handoff)
  } catch {
    return stop('the hand-off could not be kept on disk')
  }
  try {
    handoff = { ...result.handoff, createdAt: await $.clock.now() }
    await $.store.set(HANDOFF_KEY, handoff)
  } catch {
    await dropHandoff($)
    return stop('the hand-off could not be kept on disk')
  }
  try {
    await update($, handoffAtom, () => handoff)
  } catch {
    // The new session would never see it (TR-11).
    await dropHandoff($)
    return stop('the hand-off could not be kept on disk')
  }
  if (!(await stillSession($, sid, gen))) {
    await dropHandoff($)
    return changed()
  }
  const now = await $.clock.now()
  await setUi($, u => withBusy(u, 'fresh-clear', now))
  armBusyTimeout($, 'fresh-clear', now)
  $.clock.after(0, () => void runClear($, now, handoff, sid, gen))
}

async function runClear($: EngineInterface, since: number, handoff: Handoff, sid: string, gen: number): Promise<void> {
  const ours = (h: Handoff | null) => h !== null && h.fromSessionId === handoff.fromSessionId && h.createdAt === handoff.createdAt
  const fail = async (line: string): Promise<void> => {
    const held = await attempt(() => read($, handoffAtom), null)
    if (ours(held)) await dropHandoff($)
    const now = await $.clock.now()
    await setUi($, u => withNote(u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u, line, now))
    toast($, line)
  }
  // The session changed while the clear waited its turn (TR-01): clear nothing.
  if (gen !== sessionGen || cleanId(await attempt(() => $.session.id(), '')) !== sid) {
    const held = await attempt(() => read($, handoffAtom), null)
    if (ours(held)) await dropHandoff($)
    const now = await $.clock.now()
    await setUi($, u => (u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u))
    toast($, 'Start fresh stopped: the session changed.')
    return
  }
  const running = await runningCall($)
  if (running || engineCall) return fail(`Start fresh stopped: ${(running ?? engineCall) === 'compact' ? 'the last clean-up is still running' : 'the last clear is still running'}. Nothing was cleared.`)
  // Claimed with no await between the check and the claim; recorded before the call.
  const mine = await claimCall($, 'clear')
  if (!mine) return fail('Start fresh could not clear. Your conversation is unchanged.')
  let cleared = false
  try {
    await $.command.run({ command: 'clear' })
    cleared = true
  } catch {
    // Reported below, once the call has settled.
  } finally {
    await releaseCall($, mine)
  }
  if (!cleared) return fail('Start fresh could not clear. Your conversation is unchanged.')
  // The hand-off now waits for the first prompt of a conversation started since the save (KTD10).
  const now = await $.clock.now()
  await setUi($, u => (u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u))
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
  // Guards read, then write, across awaits: one press at a time (TR-02).
  const now = await attempt(() => $.clock.now(), 0)
  if (claim !== null && now - claim.at < BUSY_TIMEOUT_MS) return
  const mine = { at: now }
  claim = mine
  try {
    if (id === 'clean' || id === 'clean-first') await cleanUp($)
    else if (id === 'fresh') await startFresh($)
    else await keepWarm($)
  } catch {
    toast($, 'That did not work. Try again in a moment.')
  } finally {
    if (claim === mine) claim = null
  }
}

// ---- drawing (U8) ----

const ring = (p: number, color: string, track: string, cold: boolean): string => {
  const c = 2 * Math.PI * 7
  const bg = cold ? `stroke="${color}" stroke-dasharray="2.2 2.2"` : `class="t" stroke="${track}"`
  return (
    `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" ${bg}/>` +
    (p > 0 ? `<circle cx="10" cy="10" r="7" fill="none" stroke-width="3.2" stroke="${color}" stroke-linecap="round" stroke-dasharray="${((c * p) / 100).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 10 10)"/>` : '')
  )
}

/**
 * The desktop gives the band no light/dark signal (the config's theme is the
 * terminal's), so each picture follows the app's own appearance through its
 * colour-scheme query; the drawn colours are the light ones, the fallback.
 * Classes: k = ink stroke, kf = ink fill, t = track stroke, tf = track fill.
 */
const DARK_STYLE = '<style>@media (prefers-color-scheme: dark){.k{stroke:#f3f1ea}.kf{fill:#f3f1ea}.t{stroke:#4b4a46}.tf{fill:#4b4a46}}</style>'

function themed(svg: string, rootClass?: string): string {
  const open = svg.indexOf('>') + 1
  const head = rootClass ? svg.slice(0, open - 1).replace('<svg ', `<svg class="${rootClass}" `) + '>' : svg.slice(0, open)
  return head + DARK_STYLE + svg.slice(open)
}

const esc = (v: string): string => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** A mark's icon beside its ring or grade badge, as one small picture. */
function markSvg(mark: Mark, t: Tones): string {
  const color = toneColor(mark.tone, t)
  const icon = `<g transform="translate(0 2)"${mark.tone === 'none' ? ' class="k"' : ''} fill="none" stroke="${mark.tone === 'none' ? t.ink : color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${ICONS[mark.icon]}</g>`
  const right =
    mark.badge !== undefined
      ? `<rect x="20" y="1" width="18" height="18" rx="5"${mark.tone === 'none' ? ' class="tf"' : ''} fill="${mark.tone === 'none' ? t.track : color}"/>` +
        `<text x="29" y="14" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11.5" font-weight="700" fill="${t.card}">${esc(mark.badge)}</text>`
      : `<g transform="translate(19 0)">${ring(mark.ringPercent ?? 0, color, t.track, mark.tone === 'cold')}</g>`
  return themed(`<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20" role="img" aria-label="${esc(mark.alt)}"><title>${esc(mark.alt)}</title>${icon}${right}</svg>`)
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
      return i === bars.length - 1
        ? `<rect x="${i * 6}" y="${28 - height}" width="4" height="${height}" rx="1.5" fill="${t.good}"/>`
        : `<rect class="tf" x="${i * 6}" y="${28 - height}" width="4" height="${height}" rx="1.5" fill="${t.track}"/>`
    })
    .join('')
  return { source: themed(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="28" viewBox="0 0 ${width} 28" role="img" aria-label="${alt}"><title>${alt}</title>${body}</svg>`), alt, width }
}

function chevronSvg(open: boolean, ink: string): { source: string; alt: string } {
  const alt = open ? 'Session details open' : 'Session details folded'
  const svg = themed(iconSvg('chevron', ink, { alt }), 'k')
  return { source: open ? svg.replace(ICONS.chevron, `<g transform="rotate(180 8 8)">${ICONS.chevron}</g>`) : svg, alt }
}

function runsOf(D: Desktop, runs: Run[], t: Tones) {
  const { Text } = D
  return runs.map(r => (r.lose ? <Text bold color={t.bad}>{r.text}</Text> : r.strong ? <Text bold>{r.text}</Text> : r.text))
}

function icon(D: Desktop, name: IconName, color: string, alt?: string) {
  const { Svg } = D
  const label = alt ?? ICON_ALT[name]
  const svg = iconSvg(name, color, { alt: label })
  return <Svg source={color === LIGHT.ink ? themed(svg, 'k') : svg} alt={label} width={16} height={16} />
}

type Model = {
  snap: Snapshot
  palette: Palette
  tones: Tones
  pose: PoseState['pose']
  sheetOpen: boolean
  savingsReason: string | null
  narrow: boolean
  /** Keep warm can run now (the one guard, R12): only then does the row offer it. */
  canWarm: boolean
  /** Clawd's pictures, bottom first: the previous pose stays beneath a new one while it fades in. */
  clawd: ClawdLayer[]
  /** While watching: one picture per look, each shown while the pointer is over its part of the band. */
  gazes: (ClawdLayer & { gaze: Gaze })[]
}

/** The hover group that turns Clawd's eyes toward one part of the band. */
const gazeScope = (g: Gaze): string => `token-optimizer-gaze-${g}`

type ClawdLayer = { key: string; source: string; alt: string }

/** How long the previous pose stays beneath a new one: the fade plus the picture's own load. */
const UNDERLAY_MS = 900
let clawdTop: ClawdLayer | null = null
let clawdUnder: (ClawdLayer & { until: number }) | null = null

/**
 * The desktop shows nothing while a changed picture loads, so a bare swap
 * blinks. Keep the old pose drawn, unchanged and under its own key, beneath
 * the new one until the new one has faded in, then drop it.
 */
function clawdLayers($: EngineInterface, layer: ClawdLayer, now: number): ClawdLayer[] {
  if (clawdTop !== null && clawdTop.key !== layer.key) {
    clawdUnder = { ...clawdTop, until: now + UNDERLAY_MS }
    $.clock.after(UNDERLAY_MS + 50, () => void attempt(() => update($, frameAtom, n => (n ?? 0) + 1), undefined))
  }
  clawdTop = layer
  if (clawdUnder !== null && (clawdUnder.until <= now || clawdUnder.key === layer.key)) clawdUnder = null
  return clawdUnder !== null ? [clawdUnder, layer] : [layer]
}

/** Below this, "saved this session" is noise and stays off the row. */
const SESSION_SAVED_MIN = 1000

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
  const bars = m.narrow ? detail.savings.bars.slice(-14) : detail.savings.bars
  // A card action joins the row only when the moment calls for it (quality sagging, the cache
  // about to drop) and the sentence's own button is not already offering it.
  const q = snap.quality
  const sagging = q !== null && (q.score < QUALITY_FLOOR || q.compactions >= COMPACT_HEAVY)
  const rowActions = [
    ...(sagging ? cardList.find(c => c.id === 'quality')?.actions ?? [] : []),
    ...(m.canWarm && snap.cache.state === 'warning' ? [{ id: 'warm' as const, label: 'Keep warm' }] : []),
  ].filter(a => a.id !== say.action?.id && !(a.id === 'clean' && say.action?.id === 'clean-first'))

  return (
    <Box flexDirection="row" alignItems="flex-start" columnGap={2} paddingX={1}>
      <Box flexDirection="column" alignItems="flex-start" flexShrink={0}>
        {/* Box sizes count text cells on desktop, so the bottom picture sizes the stack and the new one sits over it. */}
        <Box position="relative">
          {m.clawd.map((c, i) => (
            <Box key={c.key} {...(i === 0 ? {} : { position: 'absolute' as const, top: 0, left: 0 })}>
              {/* Not isInteractive: the desktop reloads an interactive picture on every redraw (a blank frame); a plain one keeps its animation. */}
              <Svg source={c.source} alt={c.alt} width={72} height={57} />
            </Box>
          ))}
          {/* Hover can reveal but not move: each look is its own picture, drawn hidden over him and shown by its part of the band. */}
          {m.gazes.map(g => (
            <Box key={g.key} position="absolute" top={0} left={0} display="none" hover={{ scope: gazeScope(g.gaze), display: 'flex' }}>
              <Svg source={g.source} alt={g.alt} width={72} height={57} />
            </Box>
          ))}
        </Box>
        <Box flexDirection="row" alignItems="center" columnGap={0} hover={{ scope: gazeScope('down') }}>
          <Svg source={chevron.source} alt={chevron.alt} width={16} height={16} />
          <Button key="details" plain label="Details" onPress={() => on.details()} />
        </Box>
      </Box>
      <Box flexDirection="column" flexGrow={1} flexShrink={1} rowGap={1}>
        <Box flexDirection="row" alignItems="flex-start" justifyContent="space-between" columnGap={2} hover={{ scope: gazeScope('up-right') }}>
          <Box flexDirection="row" alignItems="flex-start" columnGap={1} flexShrink={1}>
            <Text bold>Token Optimizer</Text>
            {icon(D, say.icon, toneColor(say.tone, t))}
            <Text wrap="wrap">{runsOf(D, say.runs, t)}</Text>
          </Box>
          {say.action ? <Button key="action" variant="primary" label={say.action.label} onPress={() => on.act(say.action!.id)} /> : ''}
        </Box>
        {/* One line, never wrapped: a wrapped mark's card would open over the marks above it. */}
        <Box flexDirection="row" flexWrap="nowrap" columnGap={2} hover={{ scope: gazeScope('right') }}>
          {markList.map((mark, i) => (
            <Box key={`mark-${mark.id}`} position="relative" flexDirection="row" alignItems="center" columnGap={1}>
              <Svg source={markSvg(mark, t)} alt={mark.alt} width={40} height={20} />
              <Text bold>{mark.value}</Text>
              {m.narrow ? '' : <Text>{mark.label}</Text>}
              {cardList[i] ? cardBox(D, cardList[i], i, t, on) : ''}
            </Box>
          ))}
        </Box>
        {m.sheetOpen ? (
          <Box key="row" flexDirection="row" flexWrap="wrap" alignItems="center" columnGap={3} rowGap={1} hover={{ scope: gazeScope('down-right') }}>
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
                {(detail.savings.sessionTokens ?? 0) >= SESSION_SAVED_MIN ? (
                  <Text>
                    <Text bold>{detail.savings.sessionText}</Text> saved this session
                  </Text>
                ) : (
                  ''
                )}
                <Text>
                  <Text bold>{detail.savings.last30Text}</Text> past 30 days
                </Text>
                {detail.savings.state === 'unavailable' ? <Text>{m.savingsReason ?? detail.savings.reason ?? ''}</Text> : ''}
              </Box>
            </Box>
            {rowActions.length > 0 ? <Box flexDirection="row" alignItems="center" columnGap={1}>
              {rowActions.map(a => (
                <Button key={`row-${a.id}`} label={a.label} onPress={() => on.act(a.id)} />
              ))}
            </Box> : ''}
          </Box>
        ) : (
          ''
        )}
      </Box>
    </Box>
  )
}

/** The band's side of a finished turn: Clawd, the clock, and the refreshes after it (KTD13). */
async function endTurn($: EngineInterface, reason: Extract<PoseEvent, { type: 'turn-complete' }>['reason'], agentId: string | undefined): Promise<void> {
  try {
    if (agentId !== undefined) {
      await feedPose($, { type: 'turn-complete', reason, agentId })
      return
    }
    await closeAsks($)
    await feedClock($, { type: 'working-changed', working: false })
    await feedPose($, { type: 'turn-complete', reason })
    await feedPose($, { type: 'working-changed', working: false })
    // Quality after each turn now; savings and the clock facts a little later.
    $.clock.after(0, () => void attempt(() => refresh($), undefined))
    statusTimer?.cancel()
    statusTimer = $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(() => refresh($, { savings: true }), undefined))
  } catch {
    // The band never breaks the turn it watched.
  }
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
    // Work begun in the old session drops its result from here on.
    sessionGen += 1
    if (e.transcript_path) transcriptPath = e.transcript_path
    const sid = cleanId(e.session_id)
    // The new conversation's own start, never this event's arrival; unknown
    // means nothing is attached or announced until a prompt can read it.
    const startedAt = await sessionStartedAt($)
    await feedClock($, { type: 'clear' })
    lastCold = null
    await feedPose($, { type: 'session-start' })
    // Every step, note and Start fresh arm belonged to the old session (TR-01, TR-07).
    await setUi($, () => initialUi())
    const held = await heldHandoff($)
    const handoff = held ? await handoffThatFits($, held, { sessionId: sid, startedAt }) : null
    await attempt(() => refresh($, { sessionId: e.session_id, reset: true, savings: true, transcript: e.transcript_path || undefined }), undefined)
    // The held hand-off replaces Token Optimizer's cross-session pointer (KTD10).
    if (handoff && result.additionalContext) {
      return { ...result, additionalContext: stripCrossSessionPointer(result.additionalContext) ?? [] }
    }
    return result
  })

  // The hand-off joins the first prompt the person sends, once (KTD10).
  on('prompt.submit', async ($, e, next) => {
    if (!active || !attachesHandoff(e.origin?.kind)) return next(e)
    const held = await heldHandoff($)
    if (!held) return next(e)
    const handoff = await handoffThatFits($, held)
    if (!handoff) return next(e)
    // Taken only once it is deleted: a failed delete attaches nothing.
    if (!(await dropHandoff($))) return next(e)
    try {
      return await next({ ...e, context: [...(e.context ?? []), handoff.text] })
    } catch (error) {
      // Not accepted: the hand-off waits for the next prompt (TR-08).
      await attempt(() => $.store.set(HANDOFF_KEY, handoff), undefined)
      await attempt(() => update($, handoffAtom, () => handoff), undefined)
      throw error
    }
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
    // The turn is over even when a hook beneath rejects: the band says so either way (TR-18).
    try {
      return await next(e)
    } finally {
      if (active) await endTurn($, e.reason, e.agentId)
    }
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
    // A stored figure of another session never shows (R15), nor its clock: a
    // resume with no session.start reads its own figures now (TR-04).
    const otherSession = stored !== null && liveSid !== '' && stored.sessionId !== liveSid
    const s = otherSession ? null : stored
    if (otherSession && resyncFor !== liveSid) {
      resyncFor = liveSid
      $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
    }
    const working = e.props.isWorking
    // The rule the first prompt applies (KTD10); dropping an expired one is left to that prompt.
    const ready = handoff !== null && handoffFate(handoff, { sessionId: liveSid, cwd: await attempt(() => $.session.cwd(), ''), startedAt: await sessionStartedAt($), now }) === 'attach'
    const shownClock = otherSession ? initialClock() : clock

    const snap: Snapshot = {
      now,
      working,
      quality: s?.quality ?? null,
      contextPercent: s?.contextPercent ?? null,
      contextTokens: s?.contextTokens ?? null,
      contextWindow: s?.contextWindow ?? null,
      fiveHour: s?.fiveHour ?? null,
      week: s?.week ?? null,
      cache: view({ ...shownClock, working }, now, planDefault(s)),
      branch: s?.branch ?? null,
      savings: s?.savings ?? null,
      savingsLoading: s?.savingsState === 'loading',
      busy: busyNow(ui, now) ?? (shownClock.warming ? 'warming' : null),
      note: noteNow(ui, now),
      handoffPending: ready,
      freshArmed: isArmed(ui, now),
      earlierCheckpoint: s?.earlierCheckpoint ?? null,
    }
    // Light pictures always; each follows the app's dark mode by itself (see DARK_STYLE).
    const palette = LIGHT
    const poseNow = pose?.pose ?? 'idle'
    const mood = moodOf(snap)
    const clawdSource = clawdSvg(poseNow, mood, { animate, palette })
    const clawd = clawdLayers(
      $,
      { key: `clawd-${poseNow}-${mood}-${animate ? 'a' : 's'}`, source: clawdSource, alt: /aria-label="([^"]*)"/.exec(clawdSource)?.[1] ?? 'Clawd' },
      now,
    )

    return drawBand(
      $.ui.resolve(e),
      {
        snap,
        palette,
        tones: tonesFor('light', palette),
        pose: pose?.pose ?? 'idle',
        sheetOpen: s?.sheetOpen ?? false,
        savingsReason: s?.savingsReason ?? null,
        narrow: e.props.bodyColumns < 90,
        canWarm: canKeepWarm({ ...shownClock, working }, now),
        clawd,
        gazes:
          poseNow === 'idle' && animate
            ? (['up-right', 'right', 'down-right', 'down'] as const).map(gaze => {
                const source = clawdSvg('idle', mood, { animate, palette, gaze, fadeIn: false })
                return { key: `gaze-${gaze}-${mood}`, source, alt: 'Clawd: watching your pointer', gaze }
              })
            : [],
      },
      {
        act: id => void act($, id),
        details: () => void toggleDetails($),
      },
    )
  })
}
