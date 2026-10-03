// The band as drawn (U8): Clawd, the sentence, five marks, hover cards and
// the unfolding row on desktop; nothing on the terminal or when switched off.
import { expect, test } from 'claude-code/testing'

import { BAND, START, stub } from './world.ts'

const RED = '#d6453d' // clawd.ts LIGHT.bad

type Finder = { findAll: (q: { type: string; text?: string }) => Promise<{ text: string; props: Record<string, unknown> }[]> }

/** Text elements showing exactly `text` (the kit's string query matches a substring). */
async function exactly(ui: Finder, text: string) {
  return (await ui.findAll({ type: 'Text', text })).filter(t => t.text === text)
}

async function passesOn(mounting: Promise<unknown>): Promise<boolean> {
  try {
    await mounting
    return false
  } catch {
    // Nothing beneath the plugin draws in a test, so a pass-on rejects.
    return true
  }
}

test('desktop draws Clawd, "Token Optimizer", the sentence and five marks', async ($, on) => {
  stub(on)
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  const svgs = await ui.findAll({ type: 'Svg' })
  expect(svgs.some(s => String(s.props.alt).startsWith('Clawd: ') && s.props.isInteractive === true)).toBe(true)
  expect((await exactly(ui, 'Token Optimizer')).length).toBeGreaterThan(0)
  expect((await exactly(ui, 'All clear.')).length).toBeGreaterThan(0)

  for (const id of ['quality', 'context', 'cache', 'fiveHour', 'week']) {
    expect(await ui.find({ key: `mark-${id}` }), id).toBeDefined()
  }
  expect((await exactly(ui, '62%')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '88')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '59:30')).length).toBeGreaterThan(0)

  // Every Svg names its state (R18).
  for (const svg of svgs) {
    expect(typeof svg.props.alt === 'string' && (svg.props.alt as string).length > 0).toBe(true)
  }
  // Text is ink: nothing grey, nothing italic (R18).
  for (const t of await ui.findAll({ type: 'Text' })) {
    expect(t.props.dimColor).toBeUndefined()
    expect(t.props.italic).toBeUndefined()
  }
})

test('the terminal draws nothing: its own status line is there', async ($, on) => {
  stub(on)
  await $.session.start({ ...START, surface: 'terminal' })
  expect(await passesOn($.ui.mount({ ...BAND, surface: 'terminal' }))).toBe(true)
})

test('a survey holds the band', async ($, on) => {
  stub(on)
  await $.session.start(START)
  expect(await passesOn($.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true }, surface: 'desktop' }))).toBe(true)
})

test('switched off, nothing is drawn on desktop', { options: { enabled: false } }, async ($, on) => {
  stub(on)
  await $.session.start(START)
  expect(await passesOn($.ui.mount({ ...BAND, surface: 'desktop' }))).toBe(true)
})

test('pressing Clawd\'s details button unfolds the row, flips the chevron, and folds it again', async ($, on) => {
  stub(on)
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const chevron = async () => (await ui.findAll({ type: 'Svg' })).map(s => String(s.props.alt)).find(alt => alt.startsWith('Session details'))

  expect((await ui.find({ key: 'details' }))?.props.label).toBe('Show session details')
  expect(await chevron()).toBe('Session details folded')
  expect(await ui.find({ key: 'row' })).toBeUndefined()

  await ui.press({ key: 'details' })
  expect((await ui.find({ key: 'details' }))?.props.label).toBe('Hide session details')
  expect(await chevron()).toBe('Session details open')
  expect(await ui.find({ key: 'row' })).toBeDefined()
  expect((await exactly(ui, 'feat/band')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '41k')).length).toBeGreaterThan(0)
  expect((await ui.findAll({ type: 'Svg' })).some(s => String(s.props.alt).startsWith('Tokens saved'))).toBe(true)

  await ui.press({ key: 'details' })
  expect(await ui.find({ key: 'row' })).toBeUndefined()
  expect(await chevron()).toBe('Session details folded')
})

test('a cold cache draws the token count bold red, and Keep warm nowhere', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, requestAgoS: 2 * 3600 }
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })

  const lose = (await exactly(ui, '620k tokens'))[0]
  expect(lose?.props.bold).toBe(true)
  expect(lose?.props.color).toBe(RED)
  expect((await ui.find({ key: 'action' }))?.props.label).toBe('Clean up first')
  const labels = (await ui.findAll({ type: 'Button' })).map(b => b.props.label)
  expect(labels.includes('Keep warm')).toBe(false)
  expect((await exactly(ui, 'cold')).length).toBeGreaterThan(0)
})

test('without savings the row shows "--" totals and the reason, and every other fact', async ($, on) => {
  const w = stub(on)
  w.status = { ...w.status, savings: null, savings_state: 'unavailable', savings_reason: 'Savings database not found.' }
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await ui.press({ key: 'details' })

  expect((await exactly(ui, '--')).length).toBe(2)
  expect((await exactly(ui, 'Savings database not found.')).length).toBeGreaterThan(0)
  expect((await ui.findAll({ type: 'Svg' })).some(s => String(s.props.alt).startsWith('Tokens saved'))).toBe(false)
  expect((await exactly(ui, 'feat/band')).length).toBeGreaterThan(0)
  expect((await exactly(ui, '1h 0m')).length).toBeGreaterThan(0)
  expect(await ui.find({ type: 'Text', text: /12 tool calls/ })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /Checkpoint saved/ })).toBeDefined()
})

test('the dark theme draws Clawd from the dark palette', async ($, on) => {
  stub(on, { theme: 'dark-daltonized' })
  await $.session.start(START)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  const clawd = (await ui.findAll({ type: 'Svg' })).find(s => String(s.props.alt).startsWith('Clawd: '))
  expect(String(clawd?.props.source)).toContain('#e08a6c') // DARK.skin
})
