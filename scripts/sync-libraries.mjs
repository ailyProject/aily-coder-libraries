import { mkdtemp, mkdir, realpath, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { createInterface } from 'node:readline/promises';
import { EnvHttpProxyAgent, fetch } from 'undici';
import { readRepositories, downloadSource } from './lib/source.mjs';
import { buildPackage, packageSlug } from './lib/package.mjs';
import { loadRegistries, publishPackage } from './lib/publish.mjs';
import { findSevenZip, run, runNpm } from './lib/process.mjs';
import { createLibraryEntry, syncLibraryIndex } from './lib/index.mjs';
import { loadIndexTargets } from './lib/index-storage.mjs';
import { unpublishLibraries } from './lib/unpublish.mjs';

const repositoryRoot = fileURLToPath(new URL('../', import.meta.url));

function integer(value, name, min, max = Number.MAX_SAFE_INTEGER) {
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < min || Number(value) > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}.`);
  }
  return Number(value);
}

async function sourceTransport(env) {
  const gitEnv = { ...env };
  const proxyNames = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'];
  let gitProxy;
  if (!proxyNames.some(name => env[name]) && !env.GIT_CONFIG_COUNT) {
    try {
      const { stdout } = await run('git', ['config', '--global', '--get', 'http.proxy'], { timeout: 5000 });
      gitProxy = stdout.trim();
    } catch {
      // No global proxy is a normal configuration.
    }
    if (gitProxy) {
      gitEnv.GIT_CONFIG_COUNT = '1';
      gitEnv.GIT_CONFIG_KEY_0 = 'http.proxy';
      gitEnv.GIT_CONFIG_VALUE_0 = gitProxy;
    }
  }
  let dispatcher;
  try {
    dispatcher = new EnvHttpProxyAgent({
      httpProxy: env.http_proxy || env.HTTP_PROXY || env.all_proxy || env.ALL_PROXY || gitProxy,
      httpsProxy: env.https_proxy || env.HTTPS_PROXY || env.all_proxy || env.ALL_PROXY || gitProxy,
      noProxy: env.no_proxy || env.NO_PROXY,
    });
  } catch {
    throw new Error('Cannot configure HTTP proxy; use an HTTP(S) proxy for source downloads.');
  }
  return {
    env: gitEnv,
    fetchImpl: (url, options) => fetch(url, { ...options, dispatcher }),
    close: () => dispatcher.close(),
  };
}

export async function main(argv = process.argv.slice(2)) {
  const { values, tokens } = parseArgs({ args: argv, tokens: true, options: {
    'repositories': { type: 'string', default: path.join(repositoryRoot, 'repositories.txt') },
    'output-directory': { type: 'string', default: path.join(repositoryRoot, 'dist/npm') },
    'max-repositories': { type: 'string', default: '0' },
    'workers': { type: 'string', default: '4' },
    'dry-run': { type: 'boolean', default: false },
    'unpublish': { type: 'boolean', default: false },
    'help': { type: 'boolean', short: 'h', default: false },
  } });
  if (values.help) {
    console.log('Usage: npm run sync -- [--dry-run] [--repositories FILE] [--output-directory DIR] [--max-repositories N] [--workers 1-4]');
    console.log('       npm run sync -- --unpublish [--dry-run] [--output-directory DIR]');
    console.log('Publishing requires CN_CODER_REGISTRY_URL, EU_CODER_REGISTRY_URL, CN_CODER_NPM_TOKEN and EU_CODER_NPM_TOKEN.');
    console.log('Syncs of the default repository list upload an index of successfully published libraries to RustFS and R2, including --max-repositories; see .env.npm-sync.example.');
    return 0;
  }
  if (values.unpublish) {
    const incompatible = tokens.find(token => token.kind === 'option'
      && ['repositories', 'max-repositories', 'workers'].includes(token.name));
    if (incompatible) throw new Error(`--${incompatible.name} cannot be used with --unpublish; it removes all matching registry packages.`);
    const registries = loadRegistries(process.env);
    const outputDirectory = path.resolve(values['output-directory']);
    await mkdir(outputDirectory, { recursive: true });
    const outputRoot = await realpath(outputDirectory);
    return unpublishLibraries(registries, {
      workDirectory: outputRoot,
      env: { ...process.env, npm_config_cache: path.join(outputRoot, '.npm-cache') },
      dryRun: values['dry-run'],
      confirm: async () => {
        if (!process.stdin.isTTY) throw new Error('Unpublishing requires an interactive terminal; use --unpublish --dry-run to preview.');
        const terminal = createInterface({ input: process.stdin, output: process.stdout });
        try {
          return (await terminal.question('Delete ALL versions of the packages listed above? Type UNPUBLISH to confirm: ')).trim() === 'UNPUBLISH';
        } finally {
          terminal.close();
        }
      },
    });
  }
  const limit = integer(values['max-repositories'], '--max-repositories', 0);
  const workers = integer(values.workers, '--workers', 1, 4);
  const registries = values['dry-run'] ? [] : loadRegistries(process.env);
  const repositoriesFile = path.resolve(values.repositories);
  const partial = repositoriesFile !== path.join(repositoryRoot, 'repositories.txt');
  const targets = values['dry-run'] || partial ? [] : loadIndexTargets(process.env);
  const repositories = await readRepositories(repositoriesFile);
  const selected = limit ? repositories.slice(0, limit) : repositories;
  const outputDirectory = path.resolve(values['output-directory']);
  await mkdir(outputDirectory, { recursive: true });
  const outputRoot = await realpath(outputDirectory);
  const env = { ...process.env, npm_config_cache: path.join(outputRoot, '.npm-cache') };
  const sevenZip = await findSevenZip(env);
  await Promise.all([run('git', ['--version']), run(sevenZip, ['i']), runNpm(['--version'], { env })]);
  const transport = await sourceTransport(env);
  const packageOwners = new Map();
  const entries = [];
  let cursor = 0;
  let failed = 0;
  console.log(`${values['dry-run'] ? 'Build only' : 'Build and publish'}: ${selected.length} repositories, ${workers} workers.`);
  try {
    await Promise.all(Array.from({ length: Math.min(workers, selected.length) }, async () => {
      while (cursor < selected.length) {
        const index = cursor++;
        const repository = selected[index];
        const workDirectory = await mkdtemp(path.join(outputRoot, '.work-'));
        try {
          const source = await downloadSource(repository, workDirectory, transport);
          const slug = packageSlug(source.properties.name);
          if (packageOwners.has(slug) && packageOwners.get(slug) !== repository) {
            throw new Error('Another repository produces the same npm package name.');
          }
          packageOwners.set(slug, repository);
          const result = await buildPackage(repository, source, workDirectory, { outputDirectory: outputRoot, sevenZip, env });
          console.log(`[${index + 1}/${selected.length}] Built ${result.manifest.name}@${result.manifest.version}`);
          if (!values['dry-run']) {
            const published = await publishPackage(result.tarball, result.manifest, registries, { workDirectory, env });
            for (const target of published) console.log(`  ${target.registry}: ${target.status}`);
          }
          entries.push(createLibraryEntry(result.manifest, source.properties));
        } catch (error) {
          failed++;
          console.error(`[${index + 1}/${selected.length}] ${repository}: ${error.message}`);
        } finally {
          // Only remove the temporary directory created by this process, inside the resolved output root.
          const workRoot = await realpath(workDirectory);
          if (path.dirname(workRoot) !== outputRoot || !path.basename(workRoot).startsWith('.work-')) {
            throw new Error('Temporary directory escaped the output directory.');
          }
          await rm(workRoot, { recursive: true, force: true });
        }
      }
    }));
  } finally {
    await transport.close();
  }
  const libraryIndex = await syncLibraryIndex(entries, outputRoot, {
    dryRun: values['dry-run'], partial, targets,
  });
  const skipReasons = [
    values['dry-run'] && 'dry-run',
    partial && 'custom repository list',
  ].filter(Boolean);
  console.log(`Index: ${libraryIndex.file} (${entries.length} libraries; ${libraryIndex.uploaded ? 'uploaded to RustFS and R2' : `upload skipped: ${skipReasons.join(', ')}`}).`);
  console.log(`Finished: ${selected.length - failed} succeeded, ${failed} failed. Output: ${outputRoot}`);
  return failed ? 1 : 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().then(code => { process.exitCode = code; }).catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
