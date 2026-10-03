// Data gathering and re-keying for the band (U7: KTD5, KTD6, KTD12, KTD13).
//
// The engine refuses `$` passed across an import ("$ is followed only into a
// function declared in this same file"), and a plugin has exactly one hooks
// module. So this file never sees `$`: it works over `DataIo`, a port that
// register.tsx builds from `$` in one top-level function (`dataIo($)`), each
// member spelled `$.noun.method(...)` there. That keeps every noun call where
// the engine's scan looks for it, and lets this logic run under plain Node.
//
// Every lookup is best effort: a figure that cannot be read is left null and
// the rest still fill. Nothing here registers a hook; register.tsx wires the
// events and the cadence below.
import type { TokenOptimizerDesktopSession } from '../types/index.d.ts'
import type { Quality } from '../src/contracts.ts'
import {
  parseQualityCache,
  parseStatusBar,
  parseUsage,
  resolveTokenOptimizerRoot,
  type StatusBar,
  type TokenOptimizerRoot,
} from '../src/parse.ts'

/** Redraw once a second, but only inside the cache warning window (KTD13). */
export const TICK_WARNING_MS = 1_000
/** Redraw otherwise, beside the event-driven redraws (KTD13). */
export const TICK_IDLE_MS = 30_000
/** Re-read the quality cache this often, and after each turn (KTD13). */
export const QUALITY_REFRESH_MS = 60_000
/** Run the status command this long after a turn ends (KTD13). */
export const STATUS_AFTER_TURN_MS = 5_000
/** The status command answers from its cache in well under a second warm (KTD5). */
export const STATUS_TIMEOUT_MS = 4_000
/** `git branch --show-current` is local and instant; anything slower is skipped. */
export const GIT_TIMEOUT_MS = 1_000

/** Shown as the savings reason when no Token Optimizer install is found (R6). */
export const NOT_FOUND = 'Token Optimizer not found'

const PYTHON = 'python3'

/**
 * What the gatherer needs from the engine. register.tsx answers each member
 * with the `$` call named beside it; any member may reject.
 */
export type DataIo = {
  /** `$.clock.now()` (ms) */
  now: () => Promise<number>
  /** `$.session.id()` */
  sessionId: () => Promise<string>
  /** `$.session.cwd()` */
  cwd: () => Promise<string>
  /** `$.env.get('HOME')` (the validator wants a literal variable name) */
  envHome: () => Promise<string | undefined>
  /** `$.env.get('USERPROFILE')`, the Windows home */
  envUserProfile: () => Promise<string | undefined>
  /** `$.session.usage()` */
  usage: () => Promise<unknown>
  /** `$.fs.list(path)` */
  list: (path: string) => Promise<readonly { name: string }[]>
  /** `$.fs.stat(path)` */
  stat: (path: string) => Promise<{ kind: string; mtimeMs: number }>
  /** `$.fs.read(path)` */
  read: (path: string) => Promise<string>
  /** `$.process.run(argv, init)` */
  run: (argv: string[], init: { cwd?: string; timeoutMs: number }) => Promise<{ exitCode: number; stdout: string }>
}

async function attempt<T>(work: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await work()
  } catch {
    return fallback
  }
}

/** Session ids become file names; keep only what Token Optimizer's own sanitizer keeps. */
function cleanId(id: string): string {
  return id.replace(/[^a-zA-Z0-9_-]/g, '')
}

/** True when the stored atom belongs to another session (or to none) and must be reset (KTD12). */
export function shouldReset(stored: TokenOptimizerDesktopSession | null, liveSessionId: string): boolean {
  return stored === null || stored.sessionId !== liveSessionId
}

/** The user's home, as the engine's process sees it. */
export async function readHome(io: DataIo): Promise<string> {
  return (await attempt(() => io.envHome(), undefined)) || (await attempt(() => io.envUserProfile(), undefined)) || ''
}

/**
 * The freshest `quality-cache-<sid>.json` across Token Optimizer's storage
 * directories (R16): each plugin install's data dir, then the legacy
 * `~/.claude/token-optimizer`. Newest modification time wins.
 */
export async function readQuality(io: DataIo, home: string, sid: string): Promise<Quality | null> {
  if (!home || !sid) {
    return null
  }

  const dataRoot = `${home}/.claude/plugins/data`
  const entries = await attempt(() => io.list(dataRoot), [])
  const dirs = entries
    .filter(entry => entry.name.includes('token-optimizer'))
    .map(entry => `${dataRoot}/${entry.name}/token-optimizer`)
  dirs.push(`${home}/.claude/token-optimizer`)

  let freshest: { path: string; mtimeMs: number } | null = null

  for (const dir of dirs) {
    const path = `${dir}/quality-cache-${sid}.json`
    const stat = await attempt(() => io.stat(path), null)

    if (stat && stat.kind === 'file' && (!freshest || stat.mtimeMs > freshest.mtimeMs)) {
      freshest = { path, mtimeMs: stat.mtimeMs }
    }
  }

  if (!freshest) {
    return null
  }

  const { path } = freshest
  const raw = await attempt(() => io.read(path), null)
  const nowMs = await attempt(() => io.now(), Date.now())

  return raw === null ? null : parseQualityCache(raw, nowMs / 1000)
}

/**
 * Token Optimizer's scripts (KTD6): the installed-plugins registry first, then
 * the skill install; the first whose measure.py exists. null when neither does.
 */
export async function findTokenOptimizerRoot(io: DataIo, home: string): Promise<TokenOptimizerRoot | null> {
  const registry = home ? await attempt(() => io.read(`${home}/.claude/plugins/installed_plugins.json`), null) : null

  for (const root of resolveTokenOptimizerRoot(registry, home)) {
    if (!(await attempt(() => io.stat(`${root.scriptsDir}/measure.py`), null))) {
      continue
    }

    const { runner } = root
    const hasRunner = runner !== null && (await attempt(() => io.stat(runner), null)) !== null

    return { scriptsDir: root.scriptsDir, runner: hasRunner ? runner : null }
  }

  return null
}

/** The argv for `measure.py status-bar`, through module_runner when the install has it (bytecode reuse). */
export function statusBarArgv(root: TokenOptimizerRoot, sid: string, transcript?: string): string[] {
  const launch = root.runner ? [PYTHON, root.runner, root.scriptsDir, 'measure'] : [PYTHON, `${root.scriptsDir}/measure.py`]

  return [...launch, 'status-bar', '--session', sid, '--json', ...(transcript ? ['--transcript', transcript] : [])]
}

/** `measure.py status-bar --json` (KTD5); null when it fails, times out or prints something else. */
export async function readStatusBar(
  io: DataIo,
  root: TokenOptimizerRoot,
  sid: string,
  transcript?: string,
): Promise<StatusBar | null> {
  const result = await attempt(() => io.run(statusBarArgv(root, sid, transcript), { timeoutMs: STATUS_TIMEOUT_MS }), null)

  return result && result.exitCode === 0 ? parseStatusBar(result.stdout) : null
}

/** The current git branch; null outside a repository, on a detached HEAD, or without git. */
export async function readBranch(io: DataIo, cwd: string): Promise<string | null> {
  const init = cwd ? { cwd, timeoutMs: GIT_TIMEOUT_MS } : { timeoutMs: GIT_TIMEOUT_MS }
  const result = await attempt(() => io.run(['git', 'branch', '--show-current'], init), null)
  const branch = result && result.exitCode === 0 ? result.stdout.trim() : ''

  return branch === '' ? null : branch
}

export type GatherOptions = {
  /** The live session id when the caller knows it better than `$.session.id()` (a classic SessionStart's `session_id`). */
  sessionId?: string
  /** Start over even when the id matches (a clear, KTD12). */
  reset?: boolean
  /** Run the status command (start, 5 s after a turn, row opened: KTD13, R17). */
  savings?: boolean
  /** The session's transcript, so the status command need not look it up. */
  transcript?: string
}

/**
 * The per-session part of a Snapshot. Starts from `previous` only when it
 * belongs to this session, so nothing carries over a clear (R15). Savings and
 * the clock facts come from the status command when asked for; when it is not
 * asked, fails or times out, the last known values stay (R6).
 */
export async function gather(
  io: DataIo,
  previous: TokenOptimizerDesktopSession | null,
  options: GatherOptions = {},
): Promise<TokenOptimizerDesktopSession> {
  const now = await attempt(() => io.now(), Date.now())
  const sid = cleanId(options.sessionId ?? (await attempt(() => io.sessionId(), '')))
  const base = options.reset || shouldReset(previous, sid) ? null : previous
  const cwd = await attempt(() => io.cwd(), '')
  const home = await readHome(io)
  const usage = parseUsage(await attempt(() => io.usage(), null))
  const branch = await readBranch(io, cwd)
  const quality = await readQuality(io, home, sid)

  let status: StatusBar | null = null
  let notFound = false

  if (options.savings && sid) {
    const root = await findTokenOptimizerRoot(io, home)
    notFound = root === null
    status = root ? await readStatusBar(io, root, sid, options.transcript) : null
  }

  const kept = {
    savings: base?.savings ?? null,
    savingsState: base?.savingsState ?? ('unavailable' as const),
    savingsReason: base?.savingsReason ?? null,
    lastRequestEpoch: base?.lastRequestEpoch ?? null,
    cacheLifetime: base?.cacheLifetime ?? null,
    checkpointEpoch: base?.checkpointEpoch ?? null,
  }

  const facts = notFound
    ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: NOT_FOUND, checkpointEpoch: null }
    : status
      ? {
          // While savings load, the last known figures stay on show (R6).
          savings: status.savings ?? (status.savingsState === 'loading' ? kept.savings : null),
          savingsState: status.savingsState,
          savingsReason: status.savings ? null : status.savingsReason,
          lastRequestEpoch: status.lastRequestEpoch ?? kept.lastRequestEpoch,
          cacheLifetime: status.cacheLifetime ?? kept.cacheLifetime,
          checkpointEpoch: status.checkpointEpoch,
        }
      : kept

  return {
    sessionId: sid,
    gatheredAt: now,
    quality,
    ...usage,
    branch,
    ...facts,
    // Between status runs the quality cache is the fresher checkpoint source.
    checkpointEpoch: notFound ? null : (quality?.checkpointEpoch ?? facts.checkpointEpoch),
    sheetOpen: base?.sheetOpen ?? false,
  }
}

/**
 * What to store when `fresh` lands on top of `current` (read inside
 * `update`): keeps a row toggle made while gathering, never across sessions
 * or a forced reset.
 */
export function mergeStored(
  current: TokenOptimizerDesktopSession | null,
  fresh: TokenOptimizerDesktopSession,
  reset = false,
): TokenOptimizerDesktopSession {
  const sameSession = !reset && current !== null && current.sessionId === fresh.sessionId

  return { ...fresh, sheetOpen: sameSession ? current.sheetOpen : fresh.sheetOpen }
}
