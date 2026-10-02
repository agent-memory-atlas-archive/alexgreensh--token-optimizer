import type { Register } from 'claude-code'

// Scaffold: the band hook is registered and passes through until the drawing
// lands. Every behaviour lives in ../src (pure, Node-tested); this module is
// the only file that touches `$`.
export const register: Register = on => {
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => next(e))
}
