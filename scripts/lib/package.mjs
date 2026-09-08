import { createHash } from 'node:crypto';
import { copyFile, mkdir, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { run as defaultRun, runNpm as defaultRunNpm } from './process.mjs';
import { parseProperties } from './source.mjs';

export function packageSlug(name) {
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  if (!slug || slug.length > 180) throw new Error('Arduino library name cannot produce an npm package name.');
  return slug;
}

export async function buildPackage(repository, source, workDirectory, {
  outputDirectory,
  sevenZip,
  run = defaultRun,
  runNpm = defaultRunNpm,
  env = process.env,
}) {
  const { properties } = source;
  const slug = packageSlug(properties.name);
  const libraryDirectory = path.join(workDirectory, 'source/src', slug);
  const packageDirectory = path.join(workDirectory, 'package');
  await mkdir(libraryDirectory, { recursive: true });
  await mkdir(packageDirectory);
  await run(sevenZip, ['x', '-y', source.archivePath, `-o${libraryDirectory}`]);
  const extractedProperties = parseProperties(await readFile(path.join(libraryDirectory, 'library.properties'), 'utf8'));
  if (extractedProperties.name !== properties.name || extractedProperties.version !== properties.version) {
    throw new Error('Extracted Arduino metadata differs from the selected version.');
  }

  const entries = (await readdir(libraryDirectory, { withFileTypes: true }))
    .filter(entry => entry.isFile()).map(entry => entry.name).sort();
  const licenses = entries.filter(name => /^(licen[cs]e|copying|copyright|notice)([._-].*)?$/i.test(name));
  const licenseFile = licenses.find(name => /^(licen[cs]e|copying)([._-].*)?$/i.test(name));
  for (const name of licenses) {
    await copyFile(path.join(libraryDirectory, name), path.join(packageDirectory, name));
  }
  const upstreamReadme = entries.find(name => /^readme\.(md|markdown)$/i.test(name))
    ?? entries.find(name => /^readme(\.txt)?$/i.test(name));
  const readme = upstreamReadme
    ? await readFile(path.join(libraryDirectory, upstreamReadme), 'utf8')
    : `# ${properties.name}\n\n${properties.sentence}\n\n${properties.paragraph || ''}\n`;
  await writeFile(path.join(packageDirectory, 'readme.md'),
    `<!-- Aily Coder npm package -->\n\n` +
    `Arduino source: ${repository}\n\n` +
    `Extract \`src.7z\`; the Arduino library is in \`src/${slug}/\`. ` +
    `Keep its \`library.properties\` and source files together.\n\n` +
    (properties.depends ? `Arduino dependencies (not npm packages): ${properties.depends}\n\n` : '') +
    `---\n\n${readme}`);

  const manifest = {
    name: `@aily-project-coder/lib-${slug}`,
    version: properties.version,
    description: properties.sentence,
    author: properties.author,
    repository: { type: 'git', url: repository },
    ...(properties.url ? { homepage: properties.url } : {}),
    ...(properties.license ? { license: properties.license }
      : licenseFile ? { license: `SEE LICENSE IN ${licenseFile}` } : {}),
    keywords: ['aily', 'arduino'],
    files: ['src.7z', 'readme.md', ...licenses],
  };
  await writeFile(path.join(packageDirectory, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  // Excluding timestamps makes retries produce identical source archives.
  await run(sevenZip, [
    'a', '-t7z', '-mx=9', '-mtm=off', '-mtc=off', '-mta=off',
    path.join(packageDirectory, 'src.7z'), 'src',
  ], { cwd: path.join(workDirectory, 'source') });
  const { stdout } = await runNpm([
    'pack', '--json', '--ignore-scripts', '--offline', '--pack-destination', packageDirectory,
  ], { cwd: packageDirectory, env });
  const [packed] = JSON.parse(stdout);
  if (!packed?.filename || path.basename(packed.filename) !== packed.filename) {
    throw new Error('npm pack returned an invalid archive filename.');
  }
  const destination = path.join(outputDirectory, slug, manifest.version);
  const tarball = path.join(destination, packed.filename);
  await mkdir(path.dirname(destination), { recursive: true });
  let existing;
  try {
    existing = await readFile(tarball);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (existing) {
    const generated = await readFile(path.join(packageDirectory, packed.filename));
    const hash = data => createHash('sha512').update(data).digest('hex');
    if (hash(existing) !== hash(generated)) {
      throw new Error(`Local package ${manifest.name}@${manifest.version} has different content; use a separate output directory to inspect it.`);
    }
  } else {
    await rename(packageDirectory, destination);
  }
  return { manifest, tarball, directory: destination };
}
