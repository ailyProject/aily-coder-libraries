import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { downloadSource, normalizeVersion, parseProperties, readRepositories } from '../../scripts/lib/source.mjs';
import { findSevenZip, run } from '../../scripts/lib/process.mjs';

const repository = 'https://github.com/example/library.git#variant';
const properties = (version) => `name=Example\nversion=${version}\nauthor=Example author\nmaintainer=Example maintainer\nsentence=Example library\n`;

async function temporary(t) {
  const parent = await realpath(tmpdir());
  const directory = await mkdtemp(path.join(parent, 'aily-source-test-'));
  t.after(async () => {
    const resolved = await realpath(directory);
    assert.equal(resolved, directory);
    assert.equal(path.dirname(resolved), parent);
    await rm(resolved, { recursive: true, force: true });
  });
  return directory;
}

async function fixture(t) {
  const directory = await temporary(t);
  const upstream = path.join(directory, 'upstream');
  await mkdir(upstream);
  const env = { ...process.env, GIT_CONFIG_GLOBAL: process.platform === 'win32' ? 'NUL' : '/dev/null',
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_COUNT: '0', GIT_AUTHOR_NAME: 'Fixture',
    GIT_AUTHOR_EMAIL: 'fixture@example.invalid', GIT_COMMITTER_NAME: 'Fixture',
    GIT_COMMITTER_EMAIL: 'fixture@example.invalid' };
  const git = (args) => run('git', args, { cwd: upstream, env });
  await git(['init', '--quiet', '--template=']);
  const commit = async (tag, version) => {
    await writeFile(path.join(upstream, 'library.properties'), properties(version));
    await git(['add', '.']);
    await git(['commit', '--quiet', '-m', tag]);
    await git(['tag', tag]);
    return (await git(['rev-parse', 'HEAD'])).stdout.trim();
  };
  await commit('v99', '1.2');
  await writeFile(path.join(upstream, '.gitattributes'), 'library.properties export-ignore\ncode.cpp export-subst\n');
  await writeFile(path.join(upstream, 'code.cpp'), '// $Format:%H$\n');
  const latestCommit = await commit('arbitrary-label', '2.4.0');
  await commit('v999', 'invalid');
  const calls = [];
  const localRun = async (command, args, options) => {
    calls.push(args);
    assert.equal(options.env.GIT_TERMINAL_PROMPT, '0');
    assert.equal(options.env.GIT_ASKPASS, '');
    assert.ok(args.includes('credential.helper='));
    assert.ok(args.includes('core.askPass='));
    const localArgs = args.map((argument) => argument === 'https://github.com/example/library.git' ? upstream : argument);
    return run(command, localArgs, { ...options, env: { ...options.env, GIT_ALLOW_PROTOCOL: 'file' } });
  };
  const download = (fetchImpl) => downloadSource(repository, path.join(directory, 'work'), { run: localRun, fetchImpl, env });
  return { directory, upstream, git, download, calls, latestCommit };
}

test('repository list accepts comments and identity fragments, rejects duplicate or credential URLs', async (t) => {
  const file = path.join(await temporary(t), 'repositories.txt');
  await writeFile(file, '\uFEFF# comment\n\nhttps://GitHub.com/example/library.git/#variant\nhttps://example.com/other.git\n');
  assert.deepEqual(await readRepositories(file), [repository, 'https://example.com/other.git']);
  await writeFile(file, 'https://github.com/Example/Library.git\nhttps://github.com/example/library/');
  await assert.rejects(readRepositories(file), /重复仓库/);
  for (const value of ['https://secret@example.com/repo', 'https://example.com/repo?token=secret', 'file:///repo',
    'https://example.com/repo#invalid/fragment']) {
    await writeFile(file, value);
    await assert.rejects(readRepositories(file), (error) => !error.message.includes('secret') && /HTTP/.test(error.message));
  }
});

test('properties use declared versions and enforce required metadata', () => {
  assert.equal(normalizeVersion('v1.2'), '1.2.0');
  assert.equal(normalizeVersion('2.1.0-rc.2+build'), '2.1.0-rc.2');
  assert.equal(parseProperties(`\uFEFF# header\n${properties('1')}url=https://example.com/?x=y`).url, 'https://example.com/?x=y');
  assert.throws(() => parseProperties(properties('01.2')), /version/);
  assert.throws(() => parseProperties(properties('1').replace('name=Example\n', '')), /name/);
  assert.throws(() => parseProperties(`${properties('1')}broken line`), /无效字段/);
});

test('missing GitHub release scans tag metadata and archives only the highest valid declared version', async (t) => {
  const source = await fixture(t);
  const result = await source.download(async () => ({ status: 404 }));
  assert.equal(result.tag, 'arbitrary-label');
  assert.equal(result.properties.version, '2.4.0');
  assert.equal(result.commit, source.latestCommit);
  assert.equal(source.calls.filter((args) => args.includes('archive')).length, 1);
  const unpacked = path.join(source.directory, 'unpacked');
  await run(await findSevenZip(), ['x', result.archivePath, `-o${unpacked}`, '-y']);
  assert.equal(await readFile(path.join(unpacked, 'library.properties'), 'utf8'), properties('2.4.0'));
  assert.equal(await readFile(path.join(unpacked, 'code.cpp'), 'utf8'), '// $Format:%H$\n');
  await assert.rejects(readFile(path.join(unpacked, '.git', 'config')), { code: 'ENOENT' });
});

test('GitHub Latest Release recovers from transient failures and still selects its exact tag', async (t) => {
  const source = await fixture(t);
  const signals = [];
  const result = await source.download(async (url, options) => {
    assert.equal(url, 'https://github.com/example/library/releases/latest');
    assert.equal(options.method, 'HEAD');
    signals.push(options.signal);
    if (signals.length === 1) throw Object.assign(new Error('secret proxy URL'), { name: 'TimeoutError' });
    if (signals.length === 2) return { status: 503 };
    return { ok: true, status: 200, url: 'https://github.com/example/library/releases/tag/v99' };
  });
  assert.equal(new Set(signals).size, 3);
  assert.equal(result.tag, 'v99');
  assert.equal(result.properties.version, '1.2.0');
  assert.equal(source.calls.filter((args) => args.includes('fetch')).length, 1);
});

test('unconfirmed Latest Release falls back to the highest valid tag version', async (t) => {
  const cases = [
    { name: 'network failure', attempts: 3, fetchImpl: async () => { throw new Error('secret'); } },
    { name: 'server failure', attempts: 3, fetchImpl: async () => ({ status: 503 }) },
    { name: 'access denied', attempts: 1, fetchImpl: async () => ({ status: 403 }) },
    { name: 'invalid release URL', attempts: 1, fetchImpl: async () => ({ ok: true, status: 200, url: 'https://example.invalid/secret' }) },
  ];
  for (const scenario of cases) {
    await t.test(scenario.name, async (t) => {
      const source = await fixture(t);
      const warnings = t.mock.method(console, 'warn', () => {});
      let attempts = 0;
      const result = await source.download(async (...args) => {
        attempts++;
        return scenario.fetchImpl(...args);
      });
      assert.equal(attempts, scenario.attempts);
      assert.equal(result.tag, 'arbitrary-label');
      assert.equal(result.properties.version, '2.4.0');
      assert.equal(result.commit, source.latestCommit);
      assert.equal(source.calls.filter((args) => args.includes('archive')).length, 1);
      assert.equal(warnings.mock.calls.length, 1);
      const warning = warnings.mock.calls[0].arguments.join(' ');
      assert.match(warning, /普通 tag/);
      assert.ok(!warning.includes('secret'));
    });
  }
});

test('Latest Release rejects symlinks and submodules without falling back to older tags', async (t) => {
  for (const mode of ['120000', '160000']) {
    await t.test(mode, async (t) => {
      const source = await fixture(t);
      const oid = mode === '160000' ? source.latestCommit :
        (await source.git(['rev-parse', `${source.latestCommit}:library.properties`])).stdout.trim();
      await source.git(['update-index', '--add', '--cacheinfo', `${mode},${oid},linked-source`]);
      await writeFile(path.join(source.upstream, 'library.properties'), properties('3.0.0'));
      await source.git(['add', 'library.properties']);
      await source.git(['commit', '--quiet', '-m', 'unsafe source']);
      await source.git(['tag', 'unsafe']);
      await assert.rejects(source.download(async () => ({
        ok: true, status: 200, url: 'https://github.com/example/library/releases/tag/unsafe',
      })), /symlink|submodule/);
      assert.equal(source.calls.filter((args) => args.includes('archive')).length, 0);
    });
  }
});

test('tag fallback skips a newer unsafe tree and archives the older valid version', async (t) => {
  const directory = await temporary(t);
  const older = 'a'.repeat(40);
  const newer = 'b'.repeat(40);
  let current;
  const trees = [];
  const archived = [];
  const result = await downloadSource(repository, directory, {
    fetchImpl: async () => ({ status: 404 }),
    run: async (_command, args) => {
      if (args.includes('ls-remote')) return { stdout: `${older}\trefs/tags/older\n${newer}\trefs/tags/newer\n` };
      if (args.includes('fetch')) current = args.at(-1).startsWith('+refs/tags/older:') ? older : newer;
      if (args.includes('rev-parse')) return { stdout: current };
      if (args.includes('show')) return { stdout: properties(current === older ? '1.0.0' : '2.0.0') };
      if (args.includes('ls-tree')) {
        trees.push(args.at(-1));
        return { stdout: current === older ? `100644 blob ${older}\tlibrary.properties\0` :
          `120000 blob ${newer}\tlink\0` };
      }
      if (args.includes('archive')) archived.push(args.at(-1));
      return { stdout: '' };
    },
  });
  assert.equal(result.properties.version, '1.0.0');
  assert.equal(result.commit, older);
  assert.deepEqual(trees, [older, newer]);
  assert.deepEqual(archived, [older]);
});

test('source rejects paths that escape or alias Windows extraction targets', async (t) => {
  const directory = await temporary(t);
  const oid = 'a'.repeat(40);
  const unsafePaths = ['/absolute', '../outside', '..\\outside', 'file:stream', '.GiT/config', 'folder./file']
    .map(name => [name]);
  unsafePaths.push(['Foo.h', 'foo.h'], ['Src/x.h', 'src/y.h'], ['entry', 'entry/file.h']);
  for (const names of unsafePaths) {
    await assert.rejects(downloadSource(repository, directory, {
      fetchImpl: async () => ({ ok: true, status: 200, url: 'https://github.com/example/library/releases/tag/release' }),
      run: async (_command, args) => {
        if (args.includes('ls-remote')) return { stdout: `${oid}\trefs/tags/release\n` };
        if (args.includes('rev-parse')) return { stdout: oid };
        if (args.includes('show')) return { stdout: properties('1') };
        if (args.includes('ls-tree')) return {
          stdout: ['library.properties', ...names].map(name => `100644 blob ${oid}\t${name}\0`).join(''),
        };
        assert.ok(!args.includes('archive'));
        return { stdout: '' };
      },
    }), /不安全的归档路径/);
  }
});
