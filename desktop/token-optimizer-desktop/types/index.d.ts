// The token-optimizer-desktop contract: one atom per plugin, keyed by the
// session it describes. Readers compare its sessionId to the live session and
// reset on a mismatch, so a /clear never shows the previous session's figures.
// Later units extend SessionState; null means nothing has been read yet.
export type TokenOptimizerDesktopSession = {
  sessionId: string
}

export type TokenOptimizerDesktopState = TokenOptimizerDesktopSession | null

declare module 'claude-code' {
  interface PluginState {
    'token-optimizer-desktop': TokenOptimizerDesktopState
  }
}
