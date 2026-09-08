import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { gunzipSync } from 'node:zlib';
import { buildPackage, packageSlug } from '../../scripts/lib/package.mjs';
import { parseProperties } from '../../scripts/lib/source.mjs';
import { findSevenZip, run } from '../../scripts/lib/process.mjs';

function tarFiles(buffer) {
  const files = new Map();
  const tar = gunzipSync(buffer);
  for (let offset = 0; offset < tar.length && tar[offset];) {
    const name = tar.subarray(offset, offset + 100).toString().replace(/\0.*$/, '');
    const size = parseInt(tar.subarray(offset + 124, offset + 136).toString().replace(/\0.*$/, '').trim(), 8);
    files.set(name, tar.subarray(offset + 512, offset + 512 + size));
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}

test('npm packaging preserves upstream files, excludes lifecycle hooks, and is repeatable', async (t) => {
  const parent = await realpath(os.tmpdir());
  const root = await mkdtemp(path.join(parent, 'aily-npm-package-'));
  t.after(async () => {
    assert.equal(path.dirname(await realpath(root)), parent);
    await rm(root, { recursive: true, force: true });
  });
  const sevenZip = await findSevenZip();
  const upstream = path.join(root, 'upstream');
  await mkdir(path.join(upstream, 'src'), { recursive: true });
  const propertiesText = 'name=Example Library\nversion=1.2\nauthor=Example\nmaintainer=Example\nsentence=An example.\narchitectures=*\nincludes=Example.h\ndepends=Other Library (>=1.0.0)\n';
  await writeFile(path.join(upstream, 'library.properties'), propertiesText);
  await writeFile(path.join(upstream, 'src/Example.h'), '#pragma once\n');
  await writeFile(path.join(upstream, 'README.md'), '# Upstream README\n');
  await writeFile(path.join(upstream, 'LICENSE.txt'), 'Original license text\n');
  await writeFile(path.join(upstream, 'package.json'), JSON.stringify({ scripts: { prepack: 'node -e "process.exit(42)"' } }));
  const archivePath = path.join(root, 'source.zip');
  await run(sevenZip, ['a', '-tzip', archivePath, '.'], { cwd: upstream });
  const source = { archivePath, properties: parseProperties(propertiesText) };
  const outputDirectory = path.join(root, 'output');
  const env = { ...process.env, npm_config_cache: path.join(root, 'cache') };
  const work = await mkdtemp(path.join(root, 'work-'));
  const result = await buildPackage('https://example.invalid/library.git', source, work, { outputDirectory, sevenZip, env });
  assert.equal(result.manifest.name, '@aily-project-coder/lib-example-library');
  assert.equal(result.manifest.version, '1.2.0');
  assert.equal(result.manifest.license, 'SEE LICENSE IN LICENSE.txt');
  assert.equal(result.manifest.scripts, undefined);
  assert.equal(result.manifest.private, undefined);
  assert.equal(result.manifest.dependencies, undefined);
  const readme = await readFile(path.join(result.directory, 'readme.md'), 'utf8');
  assert.match(readme, /# Upstream README/);
  assert.match(readme, /Other Library \(>=1.0.0\)/);
  const firstArchive = await readFile(result.tarball);
  const files = tarFiles(firstArchive);
  assert.deepEqual([...files.keys()].sort(), ['package/LICENSE.txt', 'package/package.json', 'package/readme.md', 'package/src.7z']);
  assert.equal(files.get('package/LICENSE.txt').toString(), 'Original license text\n');
  assert.equal(JSON.parse(files.get('package/package.json')).name, result.manifest.name);
  const extracted = path.join(root, 'extracted');
  await run(sevenZip, ['x', '-y', path.join(result.directory, 'src.7z'), `-o${extracted}`]);
  assert.equal(await readFile(path.join(extracted, 'src/example-library/library.properties'), 'utf8'), propertiesText);
  assert.equal(await readFile(path.join(extracted, 'src/example-library/src/Example.h'), 'utf8'), '#pragma once\n');
  assert.ok((await readdir(path.join(extracted, 'src/example-library'))).includes('package.json'));

  const secondWork = await mkdtemp(path.join(root, 'work-'));
  const repeat = await buildPackage('https://example.invalid/library.git', source, secondWork, { outputDirectory, sevenZip, env });
  assert.equal(repeat.tarball, result.tarball);
  assert.deepEqual(await readFile(repeat.tarball), firstArchive);

  const conflictingWork = await mkdtemp(path.join(root, 'work-'));
  await assert.rejects(buildPackage('https://example.invalid/other.git', source, conflictingWork,
    { outputDirectory, sevenZip, env }), /Local package .* different content/);
  assert.deepEqual(await readFile(result.tarball), firstArchive);
});

test('invalid package names and mismatching extracted metadata are rejected before npm pack', async (t) => {
  assert.throws(() => packageSlug('!!!'), /cannot produce/);
  const parent = await realpath(os.tmpdir());
  const root = await mkdtemp(path.join(parent, 'aily-npm-metadata-'));
  t.after(async () => {
    assert.equal(path.dirname(await realpath(root)), parent);
    await rm(root, { recursive: true, force: true });
  });
  await assert.rejects(buildPackage('https://example.invalid/library.git', {
    archivePath: path.join(root, 'unused.zip'), properties: { name: 'Example', version: '1.0.0' },
  }, root, {
    outputDirectory: root,
    sevenZip: '7z',
    run: async (_command, args) => {
      const destination = args.find(value => value.startsWith('-o')).slice(2);
      await writeFile(path.join(destination, 'library.properties'), 'name=Other\nversion=1.0.0\nauthor=A\nmaintainer=A\nsentence=A\n');
      return { stdout: '', stderr: '' };
    },
    runNpm: async () => assert.fail('npm must not run'),
  }), /metadata differs/);
});
