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
  /** The detail row under Clawd is unfolded (R6). */
  sheetOpen: boolean
}

export type TokenOptimizerDesktopState = TokenOptimizerDesktopSession | null

/** Start fresh's hand-off, waiting for the new session's first prompt (KTD10). */
export type TokenOptimizerDesktopHandoff = {
  fromSessionId: string
  checkpointPath: string
  text: string
  /** `$.clock.now()` when it was recorded (ms). */
  createdAt: number
} | null

declare module 'claude-code' {
  interface PluginState {
    'token-optimizer-desktop': {
      session: TokenOptimizerDesktopState
      handoff: TokenOptimizerDesktopHandoff
    }
  }
}
