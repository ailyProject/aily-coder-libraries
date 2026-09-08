import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { runNpm } from '../../scripts/lib/process.mjs';
import { loadRegistries, publishPackage } from '../../scripts/lib/publish.mjs';

test('real npm publishes identical archives with per-path authentication and skips reruns', { timeout: 60_000 }, async (t) => {
  const temporaryRoot = await realpath(tmpdir());
  const directory = await mkdtemp(path.join(temporaryRoot, 'coder-publish-integration-'));
  const stored = new Map();
  const requests = [];
  const server = createServer(async (request, response) => {
    const registry = request.url.split('/')[1];
    requests.push({ method: request.method, registry, authorization: request.headers.authorization });
    const send = (status, body) => {
      response.writeHead(status, { 'content-type': 'application/json' });
      response.end(JSON.stringify(body));
    };
    if (!['cn', 'eu'].includes(registry)) return send(404, { error: 'not found' });
    if (request.method === 'GET') {
      return stored.has(registry)
        ? send(200, stored.get(registry))
        : send(404, { error: 'not found' });
    }
    if (request.method === 'PUT') {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      stored.set(registry, JSON.parse(Buffer.concat(chunks).toString('utf8')));
      return send(201, { ok: true });
    }
    send(405, { error: 'method not allowed' });
  });
  t.after(async () => {
    if (server.listening) await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    const resolved = await realpath(directory);
    assert.equal(path.dirname(resolved), temporaryRoot);
    assert.ok(path.basename(resolved).startsWith('coder-publish-integration-'));
    await rm(resolved, { recursive: true, force: true });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const env = {
    ...process.env,
    CN_CODER_REGISTRY_URL: `${origin}/cn/`,
    EU_CODER_REGISTRY_URL: `${origin}/eu/`,
    CN_CODER_NPM_TOKEN: 'fixture-cn-token',
    EU_CODER_NPM_TOKEN: 'fixture-eu-token',
    npm_config_cache: path.join(directory, 'cache'),
    npm_config_proxy: '',
    npm_config_https_proxy: '',
    NO_PROXY: '127.0.0.1',
    no_proxy: '127.0.0.1',
  };
  const fixtureDirectory = path.join(directory, 'fixture');
  await mkdir(fixtureDirectory);
  const manifest = {
    name: '@aily-project-coder/lib-publish-fixture',
    version: '1.0.0',
    files: ['readme.md'],
  };
  await writeFile(path.join(fixtureDirectory, 'package.json'), JSON.stringify(manifest));
  await writeFile(path.join(fixtureDirectory, 'readme.md'), '# Local npm publish fixture\n');
  const packed = await runNpm(['pack', '--json', '--offline', '--ignore-scripts'], { cwd: fixtureDirectory, env });
  const tarball = path.join(fixtureDirectory, JSON.parse(packed.stdout)[0].filename);
  const archive = await readFile(tarball);
  const registries = loadRegistries(env);
  const options = { workDirectory: directory, env };

  const first = await publishPackage(tarball, manifest, registries, options);
  assert.deepEqual(first.map(result => result.status), ['published', 'published']);
  for (const registry of ['cn', 'eu']) {
    const attachments = Object.values(stored.get(registry)._attachments);
    assert.equal(attachments.length, 1);
    assert.deepEqual(Buffer.from(attachments[0].data, 'base64'), archive);
  }
  const second = await publishPackage(tarball, manifest, registries, options);
  assert.deepEqual(second.map(result => result.status), ['skipped', 'skipped']);
  assert.deepEqual(requests.filter(request => request.method === 'PUT').map(request => request.registry), ['cn', 'eu']);
  assert.ok(requests.some(request => request.method === 'GET' && request.registry === 'cn'));
  assert.ok(requests.some(request => request.method === 'GET' && request.registry === 'eu'));
  for (const request of requests) {
    assert.equal(request.authorization, `Bearer fixture-${request.registry}-token`);
  }
});
