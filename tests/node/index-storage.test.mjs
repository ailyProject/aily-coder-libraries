import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, rm, rmdir, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { loadIndexTargets, uploadIndex } from '../../scripts/lib/index-storage.mjs';

const env = {
  RUSTFS_ENDPOINT: 'https://rustfs.example.test/',
  RUSTFS_ACCESS_KEY_ID: 'fixture-rustfs-id',
  RUSTFS_SECRET_ACCESS_KEY: 'fixture-rustfs-secret',
  R2_ACCOUNT_ID: 'fixture-account',
  R2_ACCESS_KEY_ID: 'fixture-r2-id',
  R2_SECRET_ACCESS_KEY: 'fixture-r2-secret',
};

test('index targets retain the existing credentials, regions, and endpoint configuration', () => {
  const targets = loadIndexTargets(env);
  assert.deepEqual(targets.map(({ name, endpoint, region }) => ({ name, endpoint, region })), [
    { name: 'RustFS', endpoint: 'https://rustfs.example.test', region: 'us-east-1' },
    { name: 'R2', endpoint: 'https://fixture-account.r2.cloudflarestorage.com', region: 'auto' },
  ]);
  const aliases = loadIndexTargets({
    ...env,
    RUSTFS_ACCESS_KEY_ID: '', RUSTFS_SECRET_ACCESS_KEY: '',
    RUSTFS_ACCESS_KEY: 'alias-id', RUSTFS_SECRET_KEY: 'alias-secret',
    RUSTFS_REGION: 'region-one', R2_REGION: 'region-two',
    R2_ACCOUNT_ID: '', R2_ENDPOINT: 'https://r2.example.test/',
  });
  assert.deepEqual(aliases[0].credentials, { accessKeyId: 'alias-id', secretAccessKey: 'alias-secret' });
  assert.equal(aliases[1].endpoint, 'https://r2.example.test');
  assert.deepEqual(aliases.map(target => target.region), ['region-one', 'region-two']);
  assert.throws(() => loadIndexTargets({ ...env, R2_SECRET_ACCESS_KEY: '' }), /R2_SECRET_ACCESS_KEY is required/);
  for (const url of ['file:///tmp/index', 'https://user:secret@example.test', 'https://example.test/?secret=value', 'https://example.test/#secret']) {
    assert.throws(() => loadIndexTargets({ ...env, RUSTFS_ENDPOINT: url }), (error) => {
      assert.match(error.message, /RUSTFS_ENDPOINT must be/);
      assert.equal(error.message.includes('secret'), false);
      return true;
    });
  }
});

async function fixture(t, failedTargets = []) {
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requests.push({ method: request.method, url: request.url, headers: request.headers, body: Buffer.concat(chunks) });
    if (failedTargets.some(target => request.url.startsWith(`/${target}/`))) {
      response.writeHead(403, { 'Content-Type': 'application/xml' });
      response.end(`<Error><Code>AccessDenied</Code><Message>${env.RUSTFS_SECRET_ACCESS_KEY}</Message></Error>`);
    } else {
      response.writeHead(200, { ETag: '"fixture-etag"' });
      response.end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const directory = await mkdtemp(join(tmpdir(), 'coder-index-storage-test-'));
  const file = join(directory, 'index.json');
  t.after(async () => { await rm(file, { force: true }); await rmdir(directory); });
  const body = Buffer.from('[{"name":"@aily-project-coder/lib-example","nickname":"示例"}]\n');
  await writeFile(file, body);
  const origin = `http://127.0.0.1:${server.address().port}`;
  const targets = loadIndexTargets({ ...env, RUSTFS_ENDPOINT: `${origin}/rustfs`, R2_ENDPOINT: `${origin}/r2` });
  return { requests, targets, file, body };
}

test('uploads identical JSON with signed path-style PUT requests to both index buckets', async (t) => {
  const { requests, targets, file, body } = await fixture(t);
  await uploadIndex(file, targets);
  assert.equal(requests.length, 2);
  for (const [index, request] of requests.entries()) {
    const target = targets[index];
    assert.equal(request.method, 'PUT');
    assert.equal(request.url.split('?')[0], `/${index ? 'r2' : 'rustfs'}/ailyblockly/libraries-coder-index.json`);
    assert.deepEqual(request.body, body);
    assert.equal(request.headers['content-type'], 'application/json');
    assert.equal(request.headers['cache-control'], 'no-store, no-cache, must-revalidate, max-age=0');
    assert.match(request.headers.authorization, /^AWS4-HMAC-SHA256 /);
    assert.ok(request.headers.authorization.includes(`Credential=${target.credentials.accessKeyId}/`));
    assert.ok(request.headers.authorization.includes(`/${target.region}/s3/aws4_request`));
    assert.equal(request.headers['x-amz-content-sha256'], createHash('sha256').update(body).digest('hex'));
    assert.equal(request.headers.authorization.includes(target.credentials.secretAccessKey), false);
  }
});

test('attempts R2 after RustFS failure and reports only failed target names', async (t) => {
  for (const failedTargets of [['rustfs'], ['rustfs', 'r2']]) {
    const { requests, targets, file } = await fixture(t, failedTargets);
    await assert.rejects(uploadIndex(file, targets), (error) => {
      assert.equal(error.message, `Index upload failed for ${failedTargets.length === 1 ? 'RustFS' : 'RustFS, R2'}; rerun to retry`);
      return true;
    });
    assert.equal(requests.length, 2);
    assert.ok(requests[1].url.startsWith('/r2/'));
  }
});
