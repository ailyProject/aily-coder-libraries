import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { loadRegistries } from '../../scripts/lib/publish.mjs';
import { unpublishLibraries } from '../../scripts/lib/unpublish.mjs';
import { main } from '../../scripts/sync-libraries.mjs';

const env = {
  CN_CODER_REGISTRY_URL: 'https://cn.example.test/npm/',
  EU_CODER_REGISTRY_URL: 'https://eu.example.test/',
  CN_CODER_NPM_TOKEN: 'fixture-cn-secret',
  EU_CODER_NPM_TOKEN: 'fixture-eu-secret',
};
const name = '@aily-project-coder/lib-example';
const registries = loadRegistries(env);
const catalog = names => Object.fromEntries(names.map(name => [name, { name, version: '1.0.0-beta.1' }]));

async function fixture(t, catalogs = [catalog([name]), catalog([name])]) {
  const directory = await mkdtemp(path.join(tmpdir(), 'coder-unpublish-test-'));
  t.after(() => rmdir(directory));
  const messages = [];
  t.mock.method(console, 'log', message => messages.push(message));
  t.mock.method(console, 'error', message => messages.push(message));
  const reads = [];
  return {
    directory, messages, reads,
    options: {
      workDirectory: directory, env,
      confirm: async () => assert.fail('unexpected confirmation'),
      runNpm: async () => assert.fail('unexpected npm invocation'),
      fetchImpl: async (url, options) => {
        const index = registries.findIndex(registry => url.href === new URL('-/all?local=1', registry.url).href);
        assert.notEqual(index, -1);
        assert.equal(options.headers.authorization, `Bearer ${env[registries[index].tokenEnv]}`);
        assert.equal(options.redirect, 'error');
        reads.push(registries[index].name);
        return { ok: true, json: async () => catalogs[index] };
      },
    },
  };
}

test('unpublish preview lists more than a search page and restricts the exact package scope', async (t) => {
  const names = Array.from({ length: 260 }, (_, index) => `@aily-project-coder/lib-example-${index}`);
  const data = { _updated: 99999, ...catalog([...names, 'unrelated', '@other/lib-example', '@aily-project/coder-lib-example', '@aily-project-coder/tool-example']) };
  const { options, directory, messages, reads } = await fixture(t, [data, {}]);
  assert.equal(await unpublishLibraries(registries, { ...options, dryRun: true }), 0);
  assert.deepEqual(reads, ['CN', 'EU']);
  assert.equal(messages.filter(message => message.startsWith('  ')).length, 260);
  assert.ok(names.every(name => messages.includes(`  ${name}`)));
  assert.deepEqual(await readdir(directory), []);
});

test('a failed or malformed catalog aborts both registries before confirmation or deletion', async (t) => {
  const { options, directory } = await fixture(t);
  for (const response of [
    { ok: false },
    { ok: true, json: async () => [] },
    { ok: true, json: async () => ({ [name]: { name: '@other/package' } }) },
    { ok: true, json: async () => catalog([`${name}@1.0.0`]) },
  ]) {
    await assert.rejects(unpublishLibraries(registries, {
      ...options,
      fetchImpl: async (url, settings) => url.hostname === 'eu.example.test' ? response : options.fetchImpl(url, settings),
    }), error => {
      assert.match(error.message, /EU registry .*no packages were unpublished/i);
      assert.equal(error.message.includes('fixture-'), false);
      return true;
    });
  }
  assert.deepEqual(await readdir(directory), []);
});

test('declining confirmation performs no npm commands', async (t) => {
  const { options, directory, reads } = await fixture(t);
  assert.equal(await unpublishLibraries(registries, {
    ...options,
    confirm: async () => { assert.deepEqual(reads, ['CN', 'EU']); return false; },
  }), 0);
  assert.deepEqual(await readdir(directory), []);
});

test('unpublish isolates authentication, continues failures, and tolerates already removed packages', async (t) => {
  const { options, directory, messages, reads } = await fixture(t);
  const calls = [];
  const settings = {
    ...options,
    confirm: async () => { assert.deepEqual(reads.slice(0, 2), ['CN', 'EU']); return true; },
    runNpm: async (args, settings) => {
      const registry = args[args.indexOf('--registry') + 1];
      calls.push(registry);
      assert.deepEqual(args.slice(0, 5), ['unpublish', name, '--force', '--ignore-scripts', '--dry-run=false']);
      assert.ok(args.includes(`--@aily-project-coder:registry=${registry}`));
      const config = await readFile(args[args.indexOf('--userconfig') + 1], 'utf8');
      assert.match(config, /\$\{CN_CODER_NPM_TOKEN\}/);
      assert.match(config, /\$\{EU_CODER_NPM_TOKEN\}/);
      assert.equal(config.includes(env.CN_CODER_NPM_TOKEN), false);
      assert.equal(config.includes(env.EU_CODER_NPM_TOKEN), false);
      assert.equal(settings.env, env);
      if (registry === registries[0].url) {
        throw Object.assign(new Error(env.CN_CODER_NPM_TOKEN), { stderr: env.EU_CODER_NPM_TOKEN });
      }
      return { stdout: '' };
    },
  };
  assert.equal(await unpublishLibraries(registries, settings), 1);
  assert.deepEqual(calls, registries.map(registry => registry.url));
  assert.equal(messages.join('\n').includes('fixture-'), false);
  assert.deepEqual(await readdir(directory), []);
  assert.equal(await unpublishLibraries(registries, {
    ...settings,
    runNpm: async () => { throw { stdout: JSON.stringify({ error: { code: 'E404' } }) }; },
  }), 0);
  assert.deepEqual(await readdir(directory), []);
});

test('unpublish rejects synchronization selectors before reading configuration or doing work', async () => {
  for (const [flag, value] of [['--repositories', 'missing.txt'], ['--max-repositories', '1'], ['--workers', '1']]) {
    await assert.rejects(main(['--unpublish', '--dry-run', flag, value]), /cannot be used with --unpublish/);
  }
});
