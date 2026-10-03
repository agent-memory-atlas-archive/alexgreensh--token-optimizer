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
export const TICK_IDLE_MS = 60_000
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
/** The installed Token Optimizer predates the status command this band reads. */
export const OUTDATED = 'Update Token Optimizer to see savings.'

/**
 * Python launchers in the order tried. The mod cannot see the host's OS, so a
 * launcher that cannot start (or Windows' Store stub, exit 9009) moves on to
 * the next: python3 on macOS and Linux, python or the `py -3` launcher on
 * Windows. The first that starts is remembered for the module's life.
 */
export const PYTHON_LAUNCHERS: readonly (readonly string[])[] = [['python3'], ['python'], ['py', '-3']]

/** Windows' "app execution alias" stub for a missing python exits with this. */
const WINDOWS_NOT_FOUND = 9009

let launcherIndex = 0

/** Forgets which launcher worked (tests, and nothing else). */
export function resetLauncher(): void {
  launcherIndex = 0
}

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
  /** `$.plugin.root`: this plugin's own folder */
  pluginRoot?: () => string
  /** `$.process.run(argv, init)` */
  run: (argv: string[], init: { cwd?: string; timeoutMs: number; stdin?: string }) => Promise<{ exitCode: number; stdout: string }>
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
  // Inside a Token Optimizer checkout (desktop/<this plugin>), the scripts beside it are the matching version.
  const own = io.pluginRoot ? trimTwo(await attempt(async () => io.pluginRoot!(), '')) : ''
  const sibling = own ? [{ scriptsDir: `${own}/skills/token-optimizer/scripts`, runner: `${own}/hooks/module_runner.py` }] : []

  for (const root of [...sibling, ...resolveTokenOptimizerRoot(registry, home)]) {
    if (!(await attempt(() => io.stat(`${root.scriptsDir}/measure.py`), null))) {
      continue
    }

    const { runner } = root
    const hasRunner = runner !== null && (await attempt(() => io.stat(runner), null)) !== null

    return { scriptsDir: root.scriptsDir, runner: hasRunner ? runner : null }
  }

  return null
}

function newer(a: number | null, b: number | null): number | null {
  return a === null ? b : b === null ? a : Math.max(a, b)
}

/** A path two folders up, or '' when it has fewer. */
function trimTwo(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts.length > 2 ? parts.slice(0, -2).join('/') : ''
}

/** The argv for `measure.py <args>`, through module_runner when the install has it (bytecode reuse). */
export function measureArgv(root: TokenOptimizerRoot, args: readonly string[], python: readonly string[] = PYTHON_LAUNCHERS[0] ?? ['python3']): string[] {
  const launch = root.runner ? [...python, root.runner, root.scriptsDir, 'measure'] : [...python, `${root.scriptsDir}/measure.py`]

  return [...launch, ...args]
}

/** The argv for `measure.py status-bar`. */
export function statusBarArgv(root: TokenOptimizerRoot, sid: string, transcript?: string, python?: readonly string[]): string[] {
  return measureArgv(root, ['status-bar', '--session', sid, '--json', ...(transcript ? ['--transcript', transcript] : [])], python)
}

function isTimeout(error: unknown): boolean {
  return /timed? ?out/i.test(error instanceof Error ? error.message : String(error))
}

/**
 * Runs `measure.py <args>` with the first Python launcher that starts. A
 * timeout is the command's own answer and is never retried, so a slow read
 * costs one timeout, not three. Rejects as the last attempt did.
 */
export async function runMeasure(
  io: DataIo,
  root: TokenOptimizerRoot,
  args: readonly string[],
  init: { timeoutMs: number; stdin?: string },
): Promise<{ exitCode: number; stdout: string }> {
  let lastError: unknown = new Error('no python launcher')

  for (let i = launcherIndex; i < PYTHON_LAUNCHERS.length; i++) {
    try {
      const result = await io.run(measureArgv(root, args, PYTHON_LAUNCHERS[i]), init)

      if (result.exitCode === WINDOWS_NOT_FOUND && i < PYTHON_LAUNCHERS.length - 1) {
        lastError = new Error('python launcher not found')
        continue
      }

      launcherIndex = i
      return result
    } catch (error) {
      if (isTimeout(error)) {
        throw error
      }

      lastError = error
    }
  }

  throw lastError
}

/**
 * `measure.py status-bar --json` (KTD5); 'outdated' when the install has no
 * such command (it prints usage, not JSON), null when it fails, times out or
 * prints something else.
 */
export async function readStatusBar(
  io: DataIo,
  root: TokenOptimizerRoot,
  sid: string,
  transcript?: string,
): Promise<StatusBar | 'outdated' | null> {
  const args = ['status-bar', '--session', sid, '--json', ...(transcript ? ['--transcript', transcript] : [])]
  const result = await attempt(() => runMeasure(io, root, args, { timeoutMs: STATUS_TIMEOUT_MS }), null)

  // An install without the command prints its usage instead of JSON (or exits 2).
  if (result && (result.exitCode === 2 || result.exitCode === 0) && !result.stdout.trimStart().startsWith('{')) return 'outdated'
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

  let status: StatusBar | 'outdated' | null = null
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
    earlierCheckpoint: base?.earlierCheckpoint ?? null,
    compactions: base?.compactions ?? null,
  }

  const facts = notFound
    ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: NOT_FOUND, checkpointEpoch: null }
    : status === 'outdated'
      ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: OUTDATED }
    : status
      ? {
          // While savings load, the last known figures stay on show (R6).
          savings: status.savings ?? (status.savingsState === 'loading' ? kept.savings : null),
          savingsState: status.savingsState,
          savingsReason: status.savings ? null : status.savingsReason,
          lastRequestEpoch: status.lastRequestEpoch ?? kept.lastRequestEpoch,
          cacheLifetime: status.cacheLifetime ?? kept.cacheLifetime,
          checkpointEpoch: status.checkpointEpoch,
          earlierCheckpoint: status.earlierCheckpoint,
          compactions: status.compactions ?? kept.compactions,
        }
      : kept

  // The higher count wins: the quality cache can lag a compaction that just landed.
  const counted = 'compactions' in facts ? facts.compactions : null
  const merged = quality && counted != null && counted > quality.compactions ? { ...quality, compactions: counted } : quality

  return {
    sessionId: sid,
    gatheredAt: now,
    quality: merged,
    ...usage,
    branch,
    ...facts,
    // The newer of the two: the quality cache knows quality saves the moment they land,
    // the status command also knows stop and compaction saves (checkpoint files).
    checkpointEpoch: notFound ? null : newer(quality?.checkpointEpoch ?? null, facts.checkpointEpoch),
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
