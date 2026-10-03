// The three buttons (U9): Clean up, Start fresh and Keep warm through
// register.tsx inside the engine, every guard on the mocked clock.
import { expect, test, type Engine } from 'claude-code/testing'

import { BAND, NOW_MS, START, stub } from './world.ts'

const sentenceOf = async (ui: { findAll: (q: { type: string }) => Promise<{ text: string }[]> }) =>
  (await ui.findAll({ type: 'Text' })).map(t => t.text)

const has = async (ui: Parameters<typeof sentenceOf>[0], text: string) => (await sentenceOf(ui)).some(t => t.includes(text))

const COMPOSER = { kind: 'composer' } as const

let lastStart: unknown
/** A clear lands: the old session ends with reason clear, then the classic SessionStart carries the new id. */
const clearLands = async ($: Engine, w: { sessionId: string }, from: string, to: string) => {
  await $.session.end({ reason: 'clear', sessionId: from, resume: {} } as never)
  w.sessionId = to
  lastStart = await $.classic.SessionStart({ source: 'clear', session_id: to } as never)
}

test('Clean up while a turn runs: no compaction and a one-line note', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  await $.turn.start({ text: 'go', turnId: 't1' })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(0)
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]?.includes('\n')).toBe(false)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
})

test('a skipped compaction returns the sentence to its rule and names the skip', async ($, on) => {
  const w = stub(on, { compact: 'skip' })
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
  expect(w.toasts.some(t => t.includes('nothing to compact'))).toBe(true)
  expect(await has(ui, 'nothing to compact')).toBe(true)
})

test('a compaction that hangs past the timeout no longer says "Cleaning up."', async ($, on) => {
  const w = stub(on, { compact: 'hang' })
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(await has(ui, 'Cleaning up.')).toBe(true)
  // A second press while busy does not start another compaction.
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)

  await w.clock.advance(120_000)
  expect(await has(ui, 'Cleaning up.')).toBe(false)
})

test('Keep warm 10 s before the deadline is refused', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 3590 }
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(0)
  expect(w.toasts.length).toBe(1)
})

test('Keep warm 200 s before the deadline forks once, however often it is pressed while warming', async ($, on) => {
  const w = stub(on, { forkDelayMs: 2000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(true)
  await ui.press({ key: 'card-cache-warm' }).catch(() => undefined)
  await w.clock.settle()
  expect(w.forks).toBe(1)

  await w.clock.advance(2000)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  // The deadline re-anchored to the warm-up: an hour from now.
  expect(await has(ui, 'All clear.')).toBe(true)
  expect(w.toasts.at(-1)).toBe('Cache kept warm.')
})

test('a warm-up that reads almost nothing finds the cache lapsed: cold, and the toast states the cost', async ($, on) => {
  const w = stub(on, { fork: { read: 10 } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(w.toasts.at(-1)).toMatch(/620k tokens at full price/)
  expect(await has(ui, 'Cache is cold.')).toBe(true)
})

test('Start fresh arms on the first press; after 5 s the next press arms again without acting', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-fresh' })
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
  await w.clock.advance(5001)
  expect(await ui.find({ key: 'action' })).toBeUndefined()

  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
  expect(w.commands).toEqual([])
})

test('Start fresh confirmed: capture, clear, then the held text joins the first prompt once, with no cross-session pointer', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  await $.classic.UserPromptSubmit({ prompt: 'hi', transcript_path: '/t/sess-1.jsonl', session_id: 'sess-1' } as never).catch(() => undefined)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(await has(ui, 'Saving checkpoint.')).toBe(true)
  // Presses while it runs are ignored.
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect(w.runs.filter(r => r.argv.includes('compact-capture')).length).toBe(1)

  await w.clock.advance(1000)
  await w.clock.settle()
  const capture = w.runs.find(r => r.argv.includes('compact-capture'))
  expect(capture?.argv.slice(-5)).toEqual(['compact-capture', '--trigger', 'start-fresh', '--budget-seconds', '25'])
  expect(JSON.parse(capture?.stdin ?? '{}').session_id).toBe('sess-1')
  expect(w.runs.find(r => r.argv.includes('resume-lean'))?.argv.slice(-3)).toEqual(['resume-lean', 'sess-1', '--print'])
  expect(w.commands).toEqual(['clear'])
  expect(await has(ui, 'Clearing.')).toBe(true)

  await clearLands($, w, 'sess-1', 'sess-2')
  const started = lastStart
  const context = (started as { additionalContext?: string[] }).additionalContext ?? []
  expect(context.join('\n').includes('Cross-session checkpoint')).toBe(false)
  expect(context.includes('Recovered notes')).toBe(true)
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)

  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
  const second = await $.prompt.submit({ text: 'again', wait: false, origin: COMPOSER })
  expect(second.context ?? []).toEqual([])
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
})

test('a failed capture, a stub checkpoint, or an empty resume queues no clear', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  for (const patch of [{ capture: 'fail' as const }, { capture: 'stub' as const }, { capture: 'ok' as const, lean: '' }]) {
    Object.assign(w, patch)
    w.toasts = []
    await ui.press({ key: 'card-quality-fresh' })
    await ui.press({ key: 'action' })
    await w.clock.settle()
    expect(w.commands).toEqual([])
    expect(w.toasts.length).toBe(1)
    expect(await has(ui, 'Saving checkpoint.')).toBe(false)
  }
})

test('a pending hand-off on disk survives a restart and joins the first prompt of the session its clear created', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', toSessionId: 'sess-1', cwd: '/work/project', checkpointPath: '/cp.md', text: 'HELD FROM BEFORE', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await has(ui, 'Checkpoint ready, it joins your first message.')).toBe(true)

  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['HELD FROM BEFORE'])
  expect(w.toasts.length).toBe(0)
  const again = await $.prompt.submit({ text: 'more', wait: false, origin: COMPOSER })
  expect(again.context ?? []).toEqual([])
})

type Mount = Awaited<ReturnType<typeof mountBand>>
const mountBand = ($: Engine) => $.ui.mount({ ...BAND, surface: 'desktop' })
const cacheAlt = async (ui: Mount) => (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt)).find(alt => alt.startsWith('Cache'))
const turnEnd = (reason: 'answer' | 'error') => ({ turnId: 't1', answer: '', durationMs: 1000, isAborted: false, reason }) as never
const confirmFresh = async (ui: Mount) => {
  await ui.press({ key: 'card-quality-fresh' })
  await ui.press({ key: 'action' })
}

test('a /clear typed while Start fresh saves its checkpoint queues no second clear (TR-01)', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(await has(ui, 'Saving checkpoint.')).toBe(true)

  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  const first = await $.prompt.submit({ text: 'new work', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a turn started while Start fresh saves its checkpoint stops it before the clear (TR-02)', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  await $.turn.start({ text: 'go', turnId: 't1' })
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts).toEqual(['Start fresh waits until the turn finishes.'])
})

test('two presses of Clean up at once start one compaction (TR-02)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await mountBand($)
  await Promise.all([ui.press({ key: 'card-quality-clean' }), ui.press({ key: 'card-quality-clean' })])
  await w.clock.settle()
  expect(w.compacts).toBe(1)
})

test('a /clear disarms Start fresh: one press in the new session arms again, never clears (TR-07)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-fresh' })
  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await ui.press({ key: 'card-quality-fresh' })
  await w.clock.settle()
  expect(w.runs.some(r => r.argv.includes('compact-capture'))).toBe(false)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Click again to clear')
})

for (const [why, held] of [
  ['saved more than 30 minutes ago', { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'TOO OLD', createdAt: NOW_MS - 31 * 60_000 }],
] as const) {
  test(`a hand-off ${why} is discarded with a one-line note, never joined (TR-05)`, async ($, on) => {
    const w = stub(on, { store: { handoff: held } })
    await $.session.start(START)
    const ui = await mountBand($)
    expect(await has(ui, 'Checkpoint ready')).toBe(false)
    const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
    expect(first.context ?? []).toEqual([])
    expect(w.toasts.length).toBe(1)
    expect(w.toasts[0]).toMatch(/^Start fresh's saved hand-off was discarded: .*\.$/)
  })
}

test('a clear still queued after 120 s keeps the hand-off and says when it will clear (TR-06)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])

  await w.clock.advance(120_000)
  expect(w.toasts.at(-1)).toBe('Start fresh clears when the current turn ends.')
  expect(await has(ui, 'Clearing.')).toBe(false)

  await clearLands($, w, 'sess-1', 'sess-2')
  const first = await $.prompt.submit({ text: 'continue', wait: false, origin: COMPOSER })
  expect(first.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a prompt rejected beneath the band keeps the hand-off for the next one (TR-08)', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', toSessionId: 'sess-1', cwd: '/work/project', checkpointPath: '/cp.md', text: 'LEAN HANDOFF TEXT', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff }, submitFails: 1 })
  await $.session.start(START)
  await expect($.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })).rejects.toBeDefined()
  const retry = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(retry.context).toEqual(['LEAN HANDOFF TEXT'])
})

test('a Keep warm that lands after a clear leaves the new session\'s clock alone (TR-09)', async ($, on) => {
  const w = stub(on, { forkDelayMs: 2000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)

  w.sessionId = 'sess-2'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(2000)
  await w.clock.settle()
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
  expect(w.toasts).toEqual([])
})

test('a Keep warm fork that never answers gives up after 120 s and can be pressed again (TR-10)', async ($, on) => {
  const w = stub(on, { forkDelayMs: 10 * 60_000 })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(await has(ui, 'Keeping the cache warm.')).toBe(true)

  await w.clock.advance(120_000)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  expect(w.toasts).toEqual(['Keep warm did not run: the request failed.'])
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
})

test('a hand-off the band cannot hold stops Start fresh before the clear (TR-11)', async ($, on) => {
  const w = stub(on, { handoffWriteFails: 100 })
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]?.startsWith('Start fresh stopped:')).toBe(true)
  // Nothing was left on disk for a later session to pick up.
  w.sessionId = 'sess-3'
  await $.session.start(START)
  const first = await $.prompt.submit({ text: 'go', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a turn that fails beneath the band still ends the turn on the band (TR-18)', async ($, on) => {
  const w = stub(on, { completeFails: 1 })
  await $.session.start(START)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await expect($.turn.complete(turnEnd('error'))).rejects.toBeDefined()
  await w.clock.settle()
  // The turn is over: Clean up runs instead of waiting for it.
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.toasts.includes('Clean up waits until the turn finishes.')).toBe(false)
  expect(w.compacts).toBe(1)
})

test('an open question from before a reload closes at the end of the turn (TR-19)', async ($, on) => {
  // A reload finds Clawd asking; the band is live but has not run a pose event yet (the theme read is slow).
  const asking = { pose: 'ask', since: NOW_MS, now: NOW_MS, working: true, rawSub: null, rawSince: NOW_MS, sub: null, agents: {}, permissions: 1, questions: 0, compacting: false, cold: false, lastActivity: NOW_MS, until: { wake: 0, done: 0, stop: 0, error: 0 } }
  const w = stub(on, { themeDelayMs: 1000, seed: { pose: asking } })
  const starting = $.session.start(START)
  await $.turn.complete(turnEnd('answer'))
  const ui = await mountBand($)
  const clawd = (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt)).find(alt => alt.startsWith('Clawd: '))
  expect(clawd).toBeDefined()
  expect(clawd).not.toBe('Clawd: needs you')
  await w.clock.advance(1000)
  await starting
})

test('Keep warm with no recorded context size names the session\'s own size when the cache lapsed (TR-21)', async ($, on) => {
  const w = stub(on, { fork: { read: 0 }, seed: { clock: { anchor: NOW_MS - 3400_000, lifetime: '1h', contextTokens: 0, working: false, warming: false, lapsed: false } } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await mountBand($)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
  expect(w.toasts.at(-1)).toMatch(/620k tokens at full price/)
})

test('a new session starts with its own clock, not the last one\'s (TR-04)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  w.sessionId = 'sess-9'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.session.start(START)
  const ui = await mountBand($)
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
  const labels = (await ui.findAll({ type: 'Button' })).map(b => b.props.label)
  expect(labels.includes('Keep warm')).toBe(false)
})

test('resuming an older session shows that session\'s own cache clock (TR-04)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  w.sessionId = 'sess-old'
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  await $.session.start(START)
  const ui = await mountBand($)
  expect((await cacheAlt(ui))?.startsWith('Cache cold')).toBe(true)
})

test('a refresh that started before a clear never brings the old clock into the new session (TR-03)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  // The savings refresh a few seconds after a turn is still waiting on the status command when /clear lands.
  w.statusDelayMs = 5000
  await $.turn.start({ text: 'go', turnId: 't1' })
  await $.turn.complete(turnEnd('answer'))
  await w.clock.advance(5000)
  w.statusDelayMs = 0
  w.sessionId = 'sess-2'
  w.status = { ...w.status, requestAgoS: null, cache_lifetime: null }
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(5000)
  await w.clock.settle()
  const ui = await mountBand($)
  expect(await cacheAlt(ui)).toBe('Cache clock starts after the first reply')
})

test('a hand-off from another project is left for its owner: not joined, not deleted, no note (R1)', async ($, on) => {
  const handoff = { fromSessionId: 'sess-a1', toSessionId: 'sess-a2', cwd: '/work/other', checkpointPath: '/cp.md', text: 'PROJECT A WORK', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  const first = await $.prompt.submit({ text: 'project b', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  expect(w.toasts).toEqual([])
  // Project A's session still finds it.
  w.sessionId = 'sess-a2'
  w.cwd = '/work/other'
  await $.session.start({ ...START, cwd: '/work/other' })
  const own = await $.prompt.submit({ text: 'project a', wait: false, origin: COMPOSER })
  expect(own.context).toEqual(['PROJECT A WORK'])
})

test('a hand-off on disk whose clear never landed does not join a plain start (TR-05)', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'NO CLEAR BEHIND IT', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  const ui = await mountBand($)
  expect(await has(ui, 'Checkpoint ready')).toBe(false)
  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
  expect(w.toasts).toEqual([])
})

test('a hand-off whose clear never landed is dropped with a note at the next start after the busy timeout (TR-05)', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'NO CLEAR BEHIND IT', createdAt: NOW_MS - 121_000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toMatch(/^Start fresh's saved hand-off was discarded: .*\.$/)
  const first = await $.prompt.submit({ text: 'go on', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a later manual /clear does not pick up the hand-off Start fresh made for another conversation (R4)', async ($, on) => {
  const w = stub(on)
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  expect(w.commands).toEqual(['clear'])
  await clearLands($, w, 'sess-1', 'sess-2')
  // The person types /clear before sending anything.
  await clearLands($, w, 'sess-2', 'sess-3')
  const first = await $.prompt.submit({ text: 'unrelated', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a manual /clear with an unlanded hand-off on disk never joins it (R4)', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', cwd: '/work/project', checkpointPath: '/cp.md', text: 'NO CLEAR BEHIND IT', createdAt: NOW_MS - 1000 }
  const w = stub(on, { store: { handoff } })
  await $.session.start(START)
  await clearLands($, w, 'sess-1', 'sess-2')
  const first = await $.prompt.submit({ text: 'fresh', wait: false, origin: COMPOSER })
  expect(first.context ?? []).toEqual([])
})

test('a Start fresh that stands down because the session changed says so (R2)', async ($, on) => {
  const w = stub(on, { captureDelayMs: 1000 })
  await $.session.start(START)
  const ui = await mountBand($)
  await confirmFresh(ui)
  await w.clock.settle()
  w.sessionId = 'sess-2'
  await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
  await w.clock.advance(1000)
  await w.clock.settle()
  expect(w.commands).toEqual([])
  expect(w.toasts).toEqual(['Start fresh stopped: the session changed.'])
})

test('a press that hangs frees the buttons once the busy timeout passes (R3)', async ($, on) => {
  const w = stub(on, { uiWriteHangs: 1 })
  await $.session.start(START)
  const ui = await mountBand($)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(0)
  await w.clock.advance(120_001)
  await ui.press({ key: 'card-quality-clean' })
  await w.clock.settle()
  expect(w.compacts).toBe(1)
})

test('a resume with no session.start shows no old clock and offers no Keep warm, then reads its own (TR-04)', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await mountBand($)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Keep warm')
  // The person resumes an older session: the id changes, no session.start fires.
  w.sessionId = 'sess-old'
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  const resumed = await mountBand($)
  expect(await cacheAlt(resumed)).toBe('Cache clock starts after the first reply')
  expect((await resumed.findAll({ type: 'Button' })).some(b => b.props.label === 'Keep warm')).toBe(false)
  await w.clock.settle()
  expect((await cacheAlt(resumed))?.startsWith('Cache cold')).toBe(true)
})

test('a warm-up left marked running by a reload is cleared, and Keep warm can run (TR-10)', async ($, on) => {
  const w = stub(on, { seed: { clock: { anchor: NOW_MS - 3400_000, lifetime: '1h', contextTokens: 620_000, working: false, warming: true, lapsed: false } } })
  w.status = { ...w.status, requestAgoS: 3400 }
  await $.session.start(START)
  const ui = await mountBand($)
  expect(await has(ui, 'Keeping the cache warm.')).toBe(false)
  await ui.press({ key: 'action' })
  await w.clock.settle()
  expect(w.forks).toBe(1)
})
