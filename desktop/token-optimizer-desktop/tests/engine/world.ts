// A session as the band sees it, for the engine tests: every noun the mod
// calls answered from memory beneath the plugin. Not a test file itself.
import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'

export const HOME = '/home/me'
export const NOW_MS = Date.parse('2026-10-03T10:00:00Z')
export const LEGACY_DIR = `${HOME}/.claude/token-optimizer`
export const TO_ROOT = '/c/to/5.13.26'
export const SCRIPTS = `${TO_ROOT}/skills/token-optimizer/scripts`
export const RUNNER = `${TO_ROOT}/hooks/module_runner.py`

export const BAND = {
  plugin: 'token-optimizer-desktop',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 120, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

export const START = { cwd: '/work/project', surface: 'desktop', isInteractive: true } as const

export type Status = {
  savings: { session_tokens: number; total_30d_tokens: number; daily: { date: string; tokens: number }[] } | null
  savings_state: string
  savings_reason: string | null
  /** Seconds before NOW_MS of the last main-thread request; null for none. */
  requestAgoS: number | null
  cache_lifetime: '1h' | '5m' | null
}

export type World = {
  sessionId: string
  files: Record<string, [mtimeMs: number, contents: string]>
  status: Status
  /** How the next compact-capture / resume-lean runs answer. */
  capture: 'ok' | 'fail' | 'stub'
  lean: string
  /** How `$.session.compact()` answers: a compaction, a skip, or never. */
  compact: 'ok' | 'skip' | 'hang'
  /** How `$.model.fork()` answers. */
  fork: { read: number } | 'nothing'
  theme: string
  /** Mocked-clock delay before compact-capture answers, and before a fork answers (ms). */
  captureDelayMs: number
  forkDelayMs: number
  /** Mocked-clock delay before the status command answers, with the figures as they stood when it was asked. */
  statusDelayMs: number
  /** How many of the next prompt submissions / turn completions beneath the band reject. */
  submitFails: number
  completeFails: number
  /** Mocked-clock delay before the theme read answers: the band is active but has not started yet. */
  themeDelayMs: number
  /** Band state (`$.state`, by atom key) as a reload finds it: served while nothing has been written. */
  seed: Record<string, unknown>
  /** How many of the next writes of a held hand-off to `$.state` fail. */
  handoffWriteFails: number
  /** What `$.store` holds at the start. */
  store: Record<string, unknown>
  runs: { argv: string[]; stdin?: string }[]
  toasts: string[]
  compacts: number
  forks: number
  commands: string[]
  clock: ReturnType<typeof mock.clock>
}

export const quality = (score: number, compactions: number) =>
  JSON.stringify({
    resource_health: score,
    resource_health_grade: 'B',
    compactions,
    tool_calls: 12,
    last_checkpoint_epoch: NOW_MS / 1000 - 600,
    session_start_ts: NOW_MS / 1000 - 3600,
  })

const DAILY = Array.from({ length: 30 }, (_, i) => ({ date: `2026-09-${String(i + 1).padStart(2, '0')}`, tokens: (i + 1) * 1000 }))

export const SAVED: Status['savings'] = { session_tokens: 41_000, total_30d_tokens: 2_400_000, daily: DAILY }

const CHECKPOINT = '/home/me/.claude/token-optimizer/checkpoints/sess-1-start-fresh.md'

const ok = (stdout: string, exitCode = 0) => ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

/** A desktop session with Token Optimizer installed, a quality cache, a warm measured cache. */
export function stub(on: On, patch: Partial<Omit<World, 'clock' | 'runs' | 'toasts' | 'compacts' | 'forks' | 'commands'>> = {}): World {
  const w: World = {
    sessionId: 'sess-1',
    files: {
      [`${HOME}/.claude/plugins/installed_plugins.json`]: [
        1,
        JSON.stringify({ version: 2, plugins: { 'token-optimizer@alexgreensh-token-optimizer': [{ scope: 'user', installPath: TO_ROOT }] } }),
      ],
      [`${SCRIPTS}/measure.py`]: [1, '#'],
      [RUNNER]: [1, '#'],
      [`${LEGACY_DIR}/quality-cache-sess-1.json`]: [1, quality(88, 0)],
    },
    status: { savings: SAVED, savings_state: 'fresh', savings_reason: null, requestAgoS: 30, cache_lifetime: '1h' },
    capture: 'ok',
    lean: 'LEAN HANDOFF TEXT',
    compact: 'ok',
    fork: { read: 600_000 },
    theme: 'light',
    captureDelayMs: 0,
    forkDelayMs: 0,
    statusDelayMs: 0,
    submitFails: 0,
    completeFails: 0,
    themeDelayMs: 0,
    seed: {},
    handoffWriteFails: 0,
    store: {},
    runs: [],
    toasts: [],
    compacts: 0,
    forks: 0,
    commands: [],
    clock: mock.clock(on, { now: NOW_MS }),
    ...patch,
  }
  const missing = (path: string) => new Error(`ENOENT: ${path}`)

  mock.store(on, w.store)
  mock.env(on, { HOME })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.SessionStart', () => ({ additionalContext: ['Recovered notes', '[Token Optimizer] Cross-session checkpoint (abcd1234): /p.md. Not your session\'s work.'] }))
  on('session.id', () => ({ value: w.sessionId }))
  on('session.cwd', () => ({ value: '/work/project' }))
  on('session.usage', () => ({
    value: {
      startedAt: 0,
      context: { window: 1_000_000, tokens: 620_000, percent: 62 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 40, resetsAt: '2026-10-03T12:00:00Z' },
        { kind: 'seven_day', percentUsed: 20 },
      ],
    },
  }))
  on('state.get', async (_, e, next) => {
    const held = await next(e)
    const seeded = e.plugin === 'token-optimizer-desktop' ? w.seed[e.key] : undefined
    return held.value?.version === 0 && held.value.value === undefined && seeded !== undefined ? { value: { value: seeded, version: 0 } } : held
  })
  on('state.set', (_, e, next) => {
    if (e.plugin === 'token-optimizer-desktop' && e.key === 'handoff' && e.value !== null && w.handoffWriteFails > 0) {
      w.handoffWriteFails -= 1
      return { value: { isSet: false as const, version: 999 } }
    }
    return next(e)
  })
  on('config.list', async () => {
    if (w.themeDelayMs) await w.clock.sleep(w.themeDelayMs)
    return { value: [{ key: 'theme', label: 'Theme', kind: 'choice', value: w.theme, provider: { plugin: 'engine', tier: 'core' } }] as never }
  })
  on('fs.list', () => ({ value: [] }))
  on('fs.stat', (_, e) => {
    const file = w.files[e.path]
    if (!file) throw missing(e.path)
    return { value: { kind: 'file' as const, size: file[1].length, mtimeMs: file[0], isLink: false } }
  })
  on('fs.read', (_, e) => {
    const file = w.files[e.path]
    if (!file) throw missing(e.path)
    return { value: file[1] }
  })
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => {
    if (w.completeFails > 0) {
      w.completeFails -= 1
      throw new Error('turn.complete failed beneath the band')
    }
    return { text: '' }
  })
  on('prompt.submit', (_, e) => {
    if (w.submitFails > 0) {
      w.submitFails -= 1
      throw new Error('prompt rejected beneath the band')
    }
    return { text: e.text, context: e.context, origin: e.origin }
  })
  on('process.run', async (_, e) => {
    const argv = [...e.argv]
    w.runs.push({ argv, stdin: e.init?.stdin })
    if (argv[0] === 'git') return ok('feat/band\n')
    if (argv.includes('status-bar')) {
      const s = w.status
      const answer = ok(
        JSON.stringify({
          schema: 1,
          savings: s.savings ? { unit: 'tokens', ...s.savings } : null,
          savings_state: s.savings_state,
          savings_reason: s.savings_reason,
          last_request_epoch: s.requestAgoS === null ? null : NOW_MS / 1000 - s.requestAgoS,
          cache_lifetime: s.cache_lifetime,
          last_checkpoint_epoch: NOW_MS / 1000 - 120,
        }),
      )
      if (w.statusDelayMs) await w.clock.sleep(w.statusDelayMs)
      return answer
    }
    if (argv.includes('compact-capture')) {
      if (w.captureDelayMs) await w.clock.sleep(w.captureDelayMs)
      if (w.capture === 'fail') return ok('', 1)
      w.files[CHECKPOINT] = [2, w.capture === 'stub' ? 'Generated: x | Note: No transcript data available\n' : '# Checkpoint\nreal work']
      return ok(`[Token Optimizer] Checkpoint saved: ${CHECKPOINT}\n`)
    }
    if (argv.includes('resume-lean')) return ok(w.lean ? `${w.lean}\n` : '', w.lean ? 0 : 1)
    return ok('', 1)
  })
  on('ui.toast', (_, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  on('session.compact', async () => {
    w.compacts += 1
    if (w.compact === 'hang') await w.clock.sleep(10 * 60_000)
    if (w.compact === 'skip') return { skip: 'nothing to compact' }
    return { messages: [] }
  })
  on('model.fork', async () => {
    w.forks += 1
    if (w.forkDelayMs) await w.clock.sleep(w.forkDelayMs)
    if (w.fork === 'nothing') return { value: { isAnswered: false, reason: 'nothing-to-fork', usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } } as never }
    return {
      value: {
        isAnswered: true,
        text: 'ok',
        usage: { input_tokens: 5, output_tokens: 1, cache_read_input_tokens: w.fork.read, cache_creation_input_tokens: 0 },
      },
    }
  })
  on('command.run', (_, e) => {
    w.commands.push(e.command)
    return { text: '' }
  })

  return w
}
