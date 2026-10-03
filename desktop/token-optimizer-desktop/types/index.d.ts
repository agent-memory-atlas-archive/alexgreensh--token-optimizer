// The token-optimizer-desktop contract (KTD10, KTD12). Plain JSON only, and
// self-contained: the engine ships this file to other plugins as is.
//
// `session` is one atom keyed by the session it describes. Readers compare its
// sessionId to the live session and reset on a mismatch, so a /clear never
// shows the previous session's figures (R15). null means nothing read yet.
//
// `handoff` is Start fresh's pending hand-off. It deliberately lives outside
// `session`: it is written in the old session and read in the new one, so a
// session reset must not wipe it.

export type TokenOptimizerDesktopLimit = { percentUsed: number; resetsAt: string | null }

export type TokenOptimizerDesktopQuality = {
  score: number
  grade: string
  drag: string | null
  toolCalls: number | null
  compactions: number
  /** Epoch seconds. */
  checkpointEpoch: number | null
  /** Epoch seconds. */
  sessionStartEpoch: number | null
}

export type TokenOptimizerDesktopSavings = {
  sessionTokens: number | null
  last30Tokens: number | null
  /** 30 entries, oldest first; today last. */
  daily: number[]
}

export type TokenOptimizerDesktopSession = {
  sessionId: string
  /** `$.clock.now()` when these figures were gathered (ms). */
  gatheredAt: number
  quality: TokenOptimizerDesktopQuality | null
  contextPercent: number | null
  contextTokens: number | null
  contextWindow: number | null
  fiveHour: TokenOptimizerDesktopLimit | null
  week: TokenOptimizerDesktopLimit | null
  /** Current git branch; null outside a repository or on a detached HEAD. */
  branch: string | null
  /** Last known savings; kept while a refresh loads or times out (R6). */
  savings: TokenOptimizerDesktopSavings | null
  savingsState: 'fresh' | 'stale' | 'loading' | 'unavailable'
  /** One short reason when savings is null (R6). */
  savingsReason: string | null
  /** Last main-thread request, epoch seconds (the cache clock's anchor). */
  lastRequestEpoch: number | null
  /** Last measured cache lifetime; null while unmeasured (R10). */
  cacheLifetime: '1h' | '5m' | null
  /** When Token Optimizer last saved a checkpoint, epoch seconds. */
  checkpointEpoch: number | null
  /** The earlier session's checkpoint flagged as resumable for this one (epoch seconds). */
  /** Compactions counted in the transcript by the status command. */
  compactions?: number | null
  earlierCheckpoint?: { epoch: number; about: string | null } | null
  /** The detail row under Clawd is unfolded (R6). */
  sheetOpen: boolean
}

export type TokenOptimizerDesktopState = TokenOptimizerDesktopSession | null

/**
 * Start fresh's hand-off, waiting for the first prompt of a session in the
 * same project that started since it was saved, within 10 minutes (KTD10).
 */
export type TokenOptimizerDesktopHandoff = {
  fromSessionId: string
  /** The project it was saved in; another project's session never touches it. */
  cwd: string
  checkpointPath: string
  text: string
  /** `$.clock.now()` when it was recorded (ms). */
  createdAt: number
} | null

/** The cache clock's reducer state (src/clock.ts ClockState, KTD8). */
export type TokenOptimizerDesktopClock = {
  anchor: number | null
  lifetime: '1h' | '5m' | null
  contextTokens: number | null
  working: boolean
  warming: boolean
  lapsed: boolean
}

/** Clawd's pose reducer state (src/pose.ts PoseState, KTD7). */
export type TokenOptimizerDesktopPose = {
  pose: 'wake' | 'idle' | 'think' | 'read' | 'type' | 'lift' | 'ask' | 'write' | 'compact' | 'done' | 'stop' | 'error' | 'cold' | 'sleep'
  since: number
  now: number
  working: boolean
  rawSub: 'think' | 'read' | 'type' | 'write' | 'lift' | null
  rawSince: number
  sub: 'think' | 'read' | 'type' | 'write' | 'lift' | null
  agents: Readonly<Record<string, boolean>>
  permissions: number
  questions: number
  compacting: boolean
  cold: boolean
  lastActivity: number
  until: Readonly<Record<'wake' | 'done' | 'stop' | 'error', number>>
}

/** What a button is doing, the last outcome, and the Start fresh arm (src/actions.ts UiState). */
export type TokenOptimizerDesktopUi = {
  busy: 'clean' | 'fresh-capture' | 'fresh-clear' | null
  busySince: number | null
  note: string | null
  noteUntil: number
  freshArmedAt: number | null
}

/**
 * The compaction or clear the buttons have running, recorded before the call
 * and cleared when it settles, so a reload mid-call still refuses a second
 * one. A record older than 10 minutes is treated as gone.
 */
export type TokenOptimizerDesktopEngineCall = {
  kind: 'compact' | 'clear'
  /** `$.clock.now()` when the call was recorded (ms). */
  startedAt: number
} | null

declare module 'claude-code' {
  interface PluginState {
    'token-optimizer-desktop': {
      session: TokenOptimizerDesktopState
      handoff: TokenOptimizerDesktopHandoff
      clock: TokenOptimizerDesktopClock | null
      pose: TokenOptimizerDesktopPose | null
      ui: TokenOptimizerDesktopUi | null
      engineCall: TokenOptimizerDesktopEngineCall
      /** The app theme's palette, from the `theme` config row. */
      theme: 'light' | 'dark'
      /** Bumped by the clock tick when the visible clock changes (KTD13). */
      frame: number
    }
  }
}
