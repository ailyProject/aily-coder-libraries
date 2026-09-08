import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, rmdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { runNpm as defaultRunNpm } from './process.mjs';

export function loadRegistries(env = process.env) {
  const registries = ['CN', 'EU'].map((name) => {
    const urlEnv = `${name}_CODER_REGISTRY_URL`;
    const tokenEnv = `${name}_CODER_NPM_TOKEN`;
    let url;
    try {
      url = new URL(env[urlEnv]);
      if (!['http:', 'https:'].includes(url.protocol)
        || url.username || url.password || url.search || url.hash) throw new Error();
    } catch {
      throw new Error(`${urlEnv} must be an HTTP(S) registry URL without credentials, query, or fragment`);
    }
    if (!env[tokenEnv]?.trim()) throw new Error(`${tokenEnv} is required`);
    url.pathname = `${url.pathname.replace(/\/+$/, '')}/`;
    return { name, url: url.href, tokenEnv };
  });
  if (registries[0].url === registries[1].url) {
    throw new Error('CN_CODER_REGISTRY_URL and EU_CODER_REGISTRY_URL must be different');
  }
  return registries;
}

export function isNotFound(error) {
  for (const output of [error.stdout, error.stderr]) {
    if (typeof output !== 'string') continue;
    try {
      if (JSON.parse(output).error?.code === 'E404') return true;
    } catch {
      if (/^npm (?:ERR!|error) code E404\r?$/m.test(output)) return true;
    }
  }
  return false;
}

function matchesArchive(dist, integrity, shasum) {
  const hashes = typeof dist?.integrity === 'string' ? dist.integrity.split(/\s+/) : [];
  if (hashes.some((hash) => hash.startsWith('sha512-'))) return hashes.includes(integrity);
  return dist?.shasum === shasum;
}

export async function withRegistryConfig(registries, {
  workDirectory,
  env = process.env,
}, action) {
  const directory = await mkdtemp(join(resolve(workDirectory), 'npm-publish-'));
  const configPath = join(directory, 'publish.npmrc');
  const packagePath = join(directory, 'package.json');
  try {
    const config = registries.map(({ url, tokenEnv }) => {
      if (!env[tokenEnv]?.trim()) throw new Error(`${tokenEnv} is required`);
      const registry = new URL(url);
      return `//${registry.host}${registry.pathname}:_authToken=\${${tokenEnv}}`;
    }).join('\n');
    await writeFile(configPath, `${config}\n`, { mode: 0o600 });
    // Anchor npm's project root here so a parent .npmrc cannot replace authentication.
    await writeFile(packagePath, '{"private":true}\n', { mode: 0o600 });
    return await action({ directory, configPath });
  } finally {
    await rm(configPath, { force: true });
    await rm(packagePath, { force: true });
    await rmdir(directory);
  }
}

export async function publishPackage(tarball, manifest, registries, {
  runNpm = defaultRunNpm,
  workDirectory,
  env = process.env,
} = {}) {
  const archivePath = resolve(tarball);
  const archive = await readFile(archivePath);
  const integrity = `sha512-${createHash('sha512').update(archive).digest('base64')}`;
  const shasum = createHash('sha1').update(archive).digest('hex');
  return withRegistryConfig(registries, { workDirectory, env }, async ({ directory, configPath }) => {
    const results = [];
    const scope = manifest.name.startsWith('@') ? manifest.name.split('/')[0] : null;
    for (const registry of registries) {
      const routing = ['--registry', registry.url, '--userconfig', configPath];
      if (scope) routing.push(`--${scope}:registry=${registry.url}`);
      const options = { cwd: directory, env };
      let existing;
      try {
        const response = await runNpm([
          'view', `${manifest.name}@${manifest.version}`, 'dist', '--json', ...routing,
        ], options);
        existing = JSON.parse(response.stdout || 'null');
      } catch (error) {
        if (!isNotFound(error)) {
          throw new Error(`${registry.name} registry lookup failed for ${manifest.name}@${manifest.version}`);
        }
      }
      if (existing !== undefined) {
        if (!matchesArchive(existing, integrity, shasum)) {
          throw new Error(`${registry.name} registry already contains different content for ${manifest.name}@${manifest.version}`);
        }
        results.push({ registry: registry.name, url: registry.url, status: 'skipped' });
        continue;
      }
      try {
        await runNpm([
          'publish', archivePath, '--ignore-scripts', '--access', 'public',
          '--tag', manifest.version.split('+', 1)[0].includes('-') ? 'next' : 'latest', ...routing,
        ], options);
      } catch {
        throw new Error(`${registry.name} registry publish failed for ${manifest.name}@${manifest.version}; rerun to resume`);
      }
      results.push({ registry: registry.name, url: registry.url, status: 'published' });
    }
    return results;
  });
}
