import assert from 'node:assert/strict';
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createLibraryEntry, INDEX_FILENAME, syncLibraryIndex } from '../../scripts/lib/index.mjs';
import { loadIndexTargets } from '../../scripts/lib/index-storage.mjs';
import { run } from '../../scripts/lib/process.mjs';

async function fixture(t) {
  const parent = await realpath(tmpdir());
  const directory = await mkdtemp(path.join(parent, 'coder-index-test-'));
  t.after(async () => {
    const resolved = await realpath(directory);
    assert.equal(path.dirname(resolved), parent);
    assert.ok(path.basename(resolved).startsWith('coder-index-test-'));
    await rm(resolved, { recursive: true, force: true });
  });
  return directory;
}

test('index entries expose npm and Arduino metadata without ZIP or Blockly fields', () => {
  const manifest = {
    name: '@aily-project-coder/lib-example-library',
    version: '1.2.0',
    description: 'An example.',
    author: 'Example',
    keywords: ['aily', 'arduino'],
    repository: { type: 'git', url: 'https://example.invalid/library.git' },
    homepage: 'https://example.invalid/library',
    license: 'MIT',
    files: ['src.7z', 'readme.md'],
    compatibility: { core: ['arduino:avr'] },
    tested: true,
  };
  const properties = {
    name: 'Example Library',
    category: 'Communication',
    architectures: ' avr, esp32 ',
    includes: 'Example.h, Other.h',
    downloadUrl: 'https://example.invalid/library.zip',
    sha256: 'legacy-checksum',
  };
  assert.deepEqual(createLibraryEntry(manifest, properties), {
    name: manifest.name,
    nickname: 'Example Library',
    version: '1.2.0',
    description: 'An example.',
    author: 'Example',
    keywords: ['aily', 'arduino'],
    repository: manifest.repository,
    category: 'Communication',
    architectures: ['avr', 'esp32'],
    providesIncludes: ['Example.h', 'Other.h'],
    homepage: manifest.homepage,
    license: 'MIT',
  });
  const minimal = createLibraryEntry({ ...manifest, homepage: undefined, license: undefined }, { name: 'Example Library' });
  assert.deepEqual(minimal.architectures, ['*']);
  assert.equal(minimal.category, 'Uncategorized');
  for (const field of ['homepage', 'license', 'providesIncludes']) assert.equal(field in minimal, false);
});

test('dry runs and custom repository lists write a sorted local index without uploading', async (t) => {
  const directory = await fixture(t);
  const entries = [{ name: '@aily-project-coder/lib-z' }, { name: '@aily-project-coder/lib-a' }];
  for (const flag of ['dryRun', 'partial']) {
    const result = await syncLibraryIndex(entries, directory, { [flag]: true });
    assert.deepEqual(result, { file: path.join(directory, INDEX_FILENAME), uploaded: false });
    assert.deepEqual(JSON.parse(await readFile(result.file, 'utf8')), { libraries: [entries[1], entries[0]] });
  }
  assert.equal(entries[0].name, '@aily-project-coder/lib-z');
});

test('selected batches upload successful entries to both targets even when other libraries fail', { timeout: 10_000 }, async (t) => {
  const directory = await fixture(t);
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requests.push({ method: request.method, body: Buffer.concat(chunks).toString('utf8') });
    response.writeHead(200);
    response.end();
  });
  t.after(async () => {
    if (server.listening) await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  const targets = loadIndexTargets({
    RUSTFS_ENDPOINT: endpoint,
    RUSTFS_ACCESS_KEY_ID: 'fixture-rustfs-id',
    RUSTFS_SECRET_ACCESS_KEY: 'fixture-rustfs-secret',
    R2_ENDPOINT: endpoint,
    R2_ACCESS_KEY_ID: 'fixture-r2-id',
    R2_SECRET_ACCESS_KEY: 'fixture-r2-secret',
  });
  for (const failed of [0, 4]) {
    requests.length = 0;
    const entries = Array.from({ length: 10 - failed }, (_, index) => ({ name: `@aily-project-coder/lib-example-${index}` }));
    const result = await syncLibraryIndex(entries, directory, { targets, failed });
    assert.equal(result.uploaded, true);
    assert.deepEqual(JSON.parse(await readFile(result.file, 'utf8')), { libraries: entries });
    assert.deepEqual(requests.map(request => request.method), ['PUT', 'PUT']);
    for (const request of requests) assert.deepEqual(JSON.parse(request.body), { libraries: entries });
  }
});

test('limited default-list publishing validates index storage before source tools or network access', async (t) => {
  const directory = await fixture(t);
  const env = {
    ...process.env,
    CN_CODER_REGISTRY_URL: 'https://cn.example.invalid/',
    EU_CODER_REGISTRY_URL: 'https://eu.example.invalid/',
    CN_CODER_NPM_TOKEN: 'fixture-cn-token',
    EU_CODER_NPM_TOKEN: 'fixture-eu-token',
    SEVEN_ZIP_PATH: path.join(directory, 'missing-seven-zip.exe'),
  };
  for (const key of Object.keys(env)) if (/^(RUSTFS_|R2_)/.test(key)) delete env[key];
  const script = fileURLToPath(new URL('../../scripts/sync-libraries.mjs', import.meta.url));
  await assert.rejects(run(process.execPath, [script, '--max-repositories', '10', '--output-directory', directory], { env }), error => {
    assert.match(error.stderr, /RUSTFS_ENDPOINT is required/);
    return true;
  });
});
