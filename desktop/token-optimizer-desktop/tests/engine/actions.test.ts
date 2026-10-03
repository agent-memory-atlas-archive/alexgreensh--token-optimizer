// The three buttons (U9): Clean up, Start fresh and Keep warm through
// register.tsx inside the engine, every guard on the mocked clock.
import { expect, test } from 'claude-code/testing'

import { BAND, NOW_MS, START, stub } from './world.ts'

const sentenceOf = async (ui: { findAll: (q: { type: string }) => Promise<{ text: string }[]> }) =>
  (await ui.findAll({ type: 'Text' })).map(t => t.text)

const has = async (ui: Parameters<typeof sentenceOf>[0], text: string) => (await sentenceOf(ui)).some(t => t.includes(text))

const COMPOSER = { kind: 'composer' } as const

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
  expect(capture?.argv.slice(-3)).toEqual(['compact-capture', '--trigger', 'start-fresh'])
  expect(JSON.parse(capture?.stdin ?? '{}').session_id).toBe('sess-1')
  expect(w.runs.find(r => r.argv.includes('resume-lean'))?.argv.slice(-3)).toEqual(['resume-lean', 'sess-1', '--print'])
  expect(w.commands).toEqual(['clear'])
  expect(await has(ui, 'Clearing.')).toBe(true)

  w.sessionId = 'sess-2'
  const started = await $.classic.SessionStart({ source: 'clear', session_id: 'sess-2' } as never)
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

test('a pending hand-off on disk survives a restart and joins the first prompt', async ($, on) => {
  const handoff = { fromSessionId: 'sess-0', checkpointPath: '/cp.md', text: 'HELD FROM BEFORE', createdAt: NOW_MS - 1000 }
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
