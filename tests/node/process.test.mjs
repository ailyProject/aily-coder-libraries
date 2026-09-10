import assert from 'node:assert/strict';
import test from 'node:test';
import { run } from '../../scripts/lib/process.mjs';

test('process timeout preserves termination metadata for Git diagnostics', async () => {
  await assert.rejects(run(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { timeout: 50 }), (error) => {
    assert.equal(error.code, null);
    assert.equal(error.signal, 'SIGTERM');
    assert.equal(error.killed, true);
    assert.equal(error.message.includes('setInterval'), false);
    return true;
  });
});
