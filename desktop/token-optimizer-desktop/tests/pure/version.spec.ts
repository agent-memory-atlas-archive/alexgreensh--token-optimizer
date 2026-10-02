// Pure tests run under Node (npm test). Named *.spec.ts, not *.test.ts:
// `claude plugin test` runs every *.test.ts under the plugin inside the
// engine, where node:test does not load. Engine tests live in tests/engine.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { PLUGIN_NAME } from '../../src/version.ts'

test('plugin name matches the manifest', () => {
  const manifest = JSON.parse(
    readFileSync(new URL('../../.claude-plugin/plugin.json', import.meta.url), 'utf8'),
  ) as { name: string }
  assert.equal(PLUGIN_NAME, manifest.name)
})
