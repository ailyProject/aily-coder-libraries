import assert from 'node:assert/strict';
import { mkdtemp, realpath, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { loadRegistries } from '../../scripts/lib/publish.mjs';
import { unpublishLibraries } from '../../scripts/lib/unpublish.mjs';
import { run } from '../../scripts/lib/process.mjs';

test('real CLI previews safely and real npm unpublishes all versions with per-registry authentication', { timeout: 60_000 }, async (t) => {
  const parent = await realpath(tmpdir());
  const directory = await mkdtemp(path.join(parent, 'coder-unpublish-integration-'));
  const packageName = '@aily-project-coder/lib-fixture';
  const unrelatedName = '@aily-project-coder/tool-fixture';
  const stored = new Map(['cn', 'eu'].map(registry => [registry, new Map([packageName, unrelatedName].map(name => [name, {
    _id: name, _rev: '1-fixture', name,
    'dist-tags': { next: '2.0.0-beta.1' },
    versions: Object.fromEntries(['1.0.0-beta.1', '2.0.0-beta.1'].map(version => [version, { name, version }])),
  }]))]));
  const requests = [];
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    const registry = url.pathname.split('/')[1];
    const send = (status, body) => {
      response.writeHead(status, { 'content-type': 'application/json' });
      response.end(JSON.stringify(body));
    };
    requests.push({ method: request.method, registry, authorization: request.headers.authorization });
    if (!stored.has(registry)) return send(404, { error: 'not found' });
    if (request.headers.authorization !== `Bearer fixture-${registry}-token`) return send(403, { error: 'forbidden' });
    const packages = stored.get(registry);
    if (request.method === 'GET' && url.pathname.endsWith('/-/all')) {
      assert.equal(url.searchParams.get('local'), '1');
      return send(200, { _updated: 99999, ...Object.fromEntries([...packages].map(([name, manifest]) => [name, {
        name, 'dist-tags': manifest['dist-tags'], version: '2.0.0-beta.1',
      }])) });
    }
    const relative = url.pathname.slice(registry.length + 2);
    const name = decodeURIComponent(relative.split('/-rev/')[0]);
    if (request.method === 'GET') return packages.has(name) ? send(200, packages.get(name)) : send(404, { error: 'not found' });
    if (request.method === 'DELETE' && relative.endsWith('/-rev/1-fixture')) {
      assert.equal(name, packageName);
      packages.delete(name);
      return send(200, { ok: true });
    }
    send(405, { error: 'method not allowed' });
  });
  t.after(async () => {
    if (server.listening) await new Promise(resolve => { server.close(resolve); server.closeAllConnections(); });
    const resolved = await realpath(directory);
    assert.equal(path.dirname(resolved), parent);
    assert.ok(path.basename(resolved).startsWith('coder-unpublish-integration-'));
    await rm(resolved, { recursive: true, force: true });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const env = {
    ...process.env,
    CN_CODER_REGISTRY_URL: `${origin}/cn/`, EU_CODER_REGISTRY_URL: `${origin}/eu/`,
    CN_CODER_NPM_TOKEN: 'fixture-cn-token', EU_CODER_NPM_TOKEN: 'fixture-eu-token',
    npm_config_cache: path.join(directory, 'cache'),
    npm_config_proxy: '', npm_config_https_proxy: '',
    NO_PROXY: '127.0.0.1', no_proxy: '127.0.0.1',
  };
  // Unpublish must work independently of source tools and object-storage configuration.
  for (const key of Object.keys(env)) if (/^(RUSTFS_|R2_)/.test(key)) delete env[key];
  env.SEVEN_ZIP_PATH = path.join(directory, 'no-seven-zip.exe');
  const script = fileURLToPath(new URL('../../scripts/sync-libraries.mjs', import.meta.url));
  const args = [script, '--unpublish', '--output-directory', directory];
  const preview = await run(process.execPath, [...args, '--dry-run'], { cwd: directory, env });
  assert.match(preview.stdout, /Preview only/);
  assert.equal(preview.stdout.includes(packageName), true);
  assert.equal(preview.stdout.includes(unrelatedName), false);
  assert.ok(requests.every(request => request.method === 'GET'));
  await assert.rejects(run(process.execPath, args, { cwd: directory, env }), error => {
    assert.match(error.stderr, /requires an interactive terminal/);
    return true;
  });
  assert.ok(requests.every(request => request.method === 'GET'));
  const registries = loadRegistries(env);
  assert.equal(await unpublishLibraries(registries, { workDirectory: directory, env, confirm: async () => true }), 0);
  assert.deepEqual(requests.filter(request => request.method === 'DELETE').map(request => request.registry), ['cn', 'eu']);
  for (const packages of stored.values()) assert.deepEqual([...packages.keys()], [unrelatedName]);
  assert.equal(await unpublishLibraries(registries, {
    workDirectory: directory, env, confirm: async () => assert.fail('empty catalog must not ask for confirmation'),
  }), 0);
  assert.equal(requests.filter(request => request.method === 'DELETE').length, 2);
});
