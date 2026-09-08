import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, readdir, rm, rmdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { loadRegistries, publishPackage } from '../../scripts/lib/publish.mjs';

const env = {
  CN_CODER_REGISTRY_URL: 'https://cn.example.test/npm',
  EU_CODER_REGISTRY_URL: 'https://eu.example.test/',
  CN_CODER_NPM_TOKEN: 'secret-cn-fixture',
  EU_CODER_NPM_TOKEN: 'secret-eu-fixture',
};
const manifest = { name: '@aily-project-coder/lib-example', version: '1.2.3+build-1' };
const archive = Buffer.from('one identical npm tarball');
const dist = {
  integrity: `sha512-${createHash('sha512').update(archive).digest('base64')}`,
  shasum: createHash('sha1').update(archive).digest('hex'),
};

function npmFailure(code) {
  return Object.assign(new Error(`npm failed: ${env.CN_CODER_NPM_TOKEN}`), {
    code: 1,
    stdout: JSON.stringify({ error: { code, summary: env.CN_CODER_NPM_TOKEN } }),
    stderr: env.EU_CODER_NPM_TOKEN,
  });
}

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'coder-publish-test-'));
  const tarball = join(directory, 'package.tgz');
  t.after(async () => {
    await rm(tarball, { force: true });
    await rmdir(directory);
  });
  await writeFile(tarball, archive);
  return { directory, tarball, registries: loadRegistries(env) };
}

test('registry configuration requires two distinct URLs and both token variables', () => {
  const registries = loadRegistries(env);
  assert.equal(registries[0].url, 'https://cn.example.test/npm/');
  assert.equal(registries[1].tokenEnv, 'EU_CODER_NPM_TOKEN');
  assert.throws(() => loadRegistries({ ...env, EU_CODER_NPM_TOKEN: '' }), /EU_CODER_NPM_TOKEN is required/);
  assert.throws(() => loadRegistries({ ...env, EU_CODER_REGISTRY_URL: `${env.CN_CODER_REGISTRY_URL}/` }), /must be different/);
  assert.throws(() => loadRegistries({ ...env, CN_CODER_REGISTRY_URL: 'https://user:secret@example.test' }), /without credentials/);
});

test('publishes the same archive sequentially with isolated placeholder authentication', async (t) => {
  const { directory, tarball, registries } = await fixture(t);
  const calls = [];
  const result = await publishPackage(tarball, manifest, registries, {
    workDirectory: directory,
    env,
    runNpm: async (args, options) => {
      calls.push(args);
      assert.equal(options.env, env);
      const config = await readFile(args[args.indexOf('--userconfig') + 1], 'utf8');
      assert.match(config, /\/\/cn\.example\.test\/npm\/:_authToken=\$\{CN_CODER_NPM_TOKEN\}/);
      assert.match(config, /\$\{EU_CODER_NPM_TOKEN\}/);
      assert.equal(JSON.stringify(args).includes(env.CN_CODER_NPM_TOKEN), false);
      for (const file of await readdir(options.cwd)) {
        const content = await readFile(join(options.cwd, file), 'utf8');
        assert.equal(content.includes(env.CN_CODER_NPM_TOKEN), false);
        assert.equal(content.includes(env.EU_CODER_NPM_TOKEN), false);
      }
      const url = args[args.indexOf('--registry') + 1];
      assert.ok(args.includes(`--@aily-project-coder:registry=${url}`));
      if (args[0] === 'view') throw npmFailure('E404');
      assert.equal(args[1], tarball);
      assert.ok(args.includes('--ignore-scripts'));
      assert.equal(args[args.indexOf('--tag') + 1], 'latest');
      return { stdout: '', stderr: '' };
    },
  });
  assert.deepEqual(calls.map((args) => args[0]), ['view', 'publish', 'view', 'publish']);
  assert.deepEqual(result.map((entry) => entry.status), ['published', 'published']);
  assert.deepEqual(await readdir(directory), ['package.tgz']);
});

test('skips matching versions using integrity or legacy shasum', async (t) => {
  const { directory, tarball, registries } = await fixture(t);
  let count = 0;
  const result = await publishPackage(tarball, manifest, registries, {
    workDirectory: directory, env,
    runNpm: async (args) => {
      assert.equal(args[0], 'view');
      return { stdout: JSON.stringify(count++ ? { shasum: dist.shasum } : dist) };
    },
  });
  assert.deepEqual(result.map((entry) => entry.status), ['skipped', 'skipped']);
});

test('refuses a different existing archive and cleans the temporary configuration', async (t) => {
  const { directory, tarball, registries } = await fixture(t);
  await assert.rejects(publishPackage(tarball, manifest, registries, {
    workDirectory: directory, env,
    runNpm: async (args) => {
      assert.equal(args[0], 'view');
      return { stdout: JSON.stringify({ integrity: 'sha512-other', shasum: dist.shasum }) };
    },
  }), /already contains different content/);
  assert.deepEqual(await readdir(directory), ['package.tgz']);
});

test('permission and network errors never trigger publishing or expose npm output', async (t) => {
  const { directory, tarball, registries } = await fixture(t);
  for (const code of ['E401', 'E403', 'ECONNRESET']) {
    let count = 0;
    await assert.rejects(publishPackage(tarball, manifest, registries, {
      workDirectory: directory, env,
      runNpm: async (args) => {
        count++;
        assert.equal(args[0], 'view');
        throw npmFailure(code);
      },
    }), (error) => {
      assert.match(error.message, /registry lookup failed/);
      assert.equal(error.message.includes('secret-'), false);
      return true;
    });
    assert.equal(count, 1);
  }
});

test('rerunning after EU failure skips CN and publishes only the missing prerelease', async (t) => {
  const { directory, tarball, registries } = await fixture(t);
  const stored = new Set();
  const publishes = [];
  let failEu = true;
  const runNpm = async (args) => {
    const url = args[args.indexOf('--registry') + 1];
    if (args[0] === 'view') {
      if (stored.has(url)) return { stdout: JSON.stringify(dist) };
      throw npmFailure('E404');
    }
    publishes.push(url);
    assert.equal(args[args.indexOf('--tag') + 1], 'next');
    if (url === registries[1].url && failEu) throw npmFailure('ECONNRESET');
    stored.add(url);
    return { stdout: '' };
  };
  const options = { workDirectory: directory, env, runNpm };
  const prerelease = { ...manifest, version: '1.2.3-beta.1' };
  await assert.rejects(publishPackage(tarball, prerelease, registries, options), /EU registry publish failed.*rerun/);
  failEu = false;
  const result = await publishPackage(tarball, prerelease, registries, options);
  assert.deepEqual(result.map((entry) => entry.status), ['skipped', 'published']);
  assert.deepEqual(publishes, [registries[0].url, registries[1].url, registries[1].url]);
});
