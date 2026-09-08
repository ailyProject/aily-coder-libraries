import { EnvHttpProxyAgent, fetch } from 'undici';
import { isNotFound, withRegistryConfig } from './publish.mjs';
import { runNpm as defaultRunNpm } from './process.mjs';

const packagePrefix = '@aily-project-coder/lib-';

async function listPackages(registry, env, fetchImpl, dispatcher) {
  // Verdaccio 6's local catalog has no search-page limit and includes next-only packages.
  const url = new URL('-/all?local=1', registry.url);
  let catalog;
  try {
    const response = await fetchImpl(url, {
      headers: { authorization: `Bearer ${env[registry.tokenEnv]}`, accept: 'application/json' },
      redirect: 'error', signal: AbortSignal.timeout(30_000), dispatcher,
    });
    if (!response.ok) throw new Error();
    catalog = await response.json();
  } catch {
    throw new Error(`${registry.name} registry package listing failed; requires Verdaccio 6 /-/all?local=1. No packages were unpublished.`);
  }
  if (!catalog || typeof catalog !== 'object' || Array.isArray(catalog)) {
    throw new Error(`${registry.name} registry returned an invalid package listing; no packages were unpublished.`);
  }
  const packages = [];
  for (const [name, metadata] of Object.entries(catalog)) {
    if (name === '_updated') continue;
    if (metadata?.name !== name) {
      throw new Error(`${registry.name} registry returned an invalid package listing; no packages were unpublished.`);
    }
    if (!name.startsWith(packagePrefix)) continue;
    if (!/^@aily-project-coder\/lib-[a-z0-9][a-z0-9._-]*$/.test(name) || name.length > 214) {
      throw new Error(`${registry.name} registry returned an invalid library package name; no packages were unpublished.`);
    }
    packages.push(name);
  }
  return packages.sort();
}

export async function unpublishLibraries(registries, {
  workDirectory,
  env = process.env,
  dryRun = false,
  confirm,
  fetchImpl = fetch,
  runNpm = defaultRunNpm,
}) {
  let dispatcher;
  try {
    dispatcher = new EnvHttpProxyAgent({
      httpProxy: env.http_proxy || env.HTTP_PROXY,
      httpsProxy: env.https_proxy || env.HTTPS_PROXY,
      noProxy: env.no_proxy || env.NO_PROXY,
    });
  } catch {
    throw new Error('Cannot configure HTTP proxy for registry package listing.');
  }
  const plans = [];
  try {
    // Discover both complete lists before asking for confirmation or deleting anything.
    for (const registry of registries) {
      plans.push({ registry, packages: await listPackages(registry, env, fetchImpl, dispatcher) });
    }
  } finally {
    await dispatcher.close();
  }
  for (const { registry, packages } of plans) {
    console.log(`${registry.name}: ${registry.url} (${packages.length} packages, ALL versions)`);
    for (const name of packages) console.log(`  ${name}`);
  }
  if (!plans.some(plan => plan.packages.length)) {
    console.log('No matching library packages found.');
    return 0;
  }
  if (dryRun) {
    console.log('Preview only; no packages were unpublished.');
    return 0;
  }
  console.log('There is no automatic undo. Restoring packages requires saved archives and registry permission.');
  if (!await confirm()) {
    console.log('Cancelled; no packages were unpublished.');
    return 0;
  }
  return withRegistryConfig(registries, { workDirectory, env }, async ({ directory, configPath }) => {
    let removed = 0;
    let missing = 0;
    let failed = 0;
    for (const { registry, packages } of plans) {
      for (const name of packages) {
        try {
          await runNpm([
            'unpublish', name, '--force', '--ignore-scripts', '--dry-run=false',
            '--registry', registry.url, '--userconfig', configPath,
            `--@aily-project-coder:registry=${registry.url}`,
          ], { cwd: directory, env });
          removed++;
          console.log(`${registry.name}: unpublished ${name} (all versions)`);
        } catch (error) {
          if (isNotFound(error)) {
            missing++;
            console.log(`${registry.name}: ${name} is already absent`);
          } else {
            failed++;
            console.error(`${registry.name}: unpublish failed for ${name}; inspect this package on the registry before retrying.`);
          }
        }
      }
    }
    console.log(`Finished: ${removed} unpublished, ${missing} already absent, ${failed} failed.`);
    return failed ? 1 : 0;
  });
}
