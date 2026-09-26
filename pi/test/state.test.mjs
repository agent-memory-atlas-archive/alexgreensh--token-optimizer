import { test } from 'node:test';
import { strict as assert } from 'node:assert';
import fs, { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { readPrivateFile } from '../src/state.ts';
// TypeScript 5.9 can transpile this source on Node 22 via strip-types? Tests import compiled after tsc in later rounds.
test('Pi package manifest declares one extension and no runtime dependency on host', () => {
 const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
 assert.deepEqual(pkg.pi.extensions, ['./extensions/token-optimizer.ts']);
 assert.equal(pkg.dependencies?.['@earendil-works/pi-coding-agent'], undefined);
 assert.equal(pkg.peerDependencies['@earendil-works/pi-coding-agent'], '*');
});

test('private reads stay bounded if the file grows after the size check', () => {
 const root = mkdtempSync(join(tmpdir(), 'pi-read-'));
 const file = join(root, 'data.txt');
 writeFileSync(file, '12345', { mode: 0o600 });
 const original = fs.fstatSync;
 try {
  fs.fstatSync = (...args) => {
   const stat = original(...args);
   stat.size = 4; // Simulate a stale pre-read size after a concurrent append.
   return stat;
  };
  syncBuiltinESMExports();
  assert.throws(() => readPrivateFile(file, 4), /exceeds size limit/);
 } finally {
  fs.fstatSync = original;
  syncBuiltinESMExports();
  rmSync(root, { recursive: true, force: true });
 }
});
