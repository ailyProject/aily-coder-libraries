import { execFile } from 'node:child_process';
import { access } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execute = promisify(execFile);

export async function run(command, args, options = {}) {
  try {
    return await execute(command, args, {
      encoding: 'utf8',
      timeout: 600_000,
      maxBuffer: 32 * 1024 * 1024,
      windowsHide: true,
      ...options,
      shell: false,
    });
  } catch (cause) {
    // Callers may inspect captured output, but command lines and raw stderr can contain credentials.
    const error = new Error(`${path.basename(command)} failed (${cause.code ?? cause.signal ?? 'unknown'})`);
    error.code = cause.code;
    error.stdout = cause.stdout ?? '';
    error.stderr = cause.stderr ?? '';
    throw error;
  }
}

export async function runNpm(args, { env = process.env, ...options } = {}) {
  const candidates = [
    process.env.npm_execpath,
    path.join(path.dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js'),
    path.resolve(path.dirname(process.execPath), '../lib/node_modules/npm/bin/npm-cli.js'),
  ].filter(Boolean);
  const npmEnv = { ...env, npm_config_logs_max: '0', npm_config_update_notifier: 'false' };
  for (const candidate of candidates) {
    try {
      await access(candidate);
    } catch {
      continue;
    }
    return run(process.execPath, [candidate, ...args], { ...options, env: npmEnv });
  }
  if (process.platform === 'win32') {
    throw new Error('Cannot locate npm-cli.js; run this script with npm run sync.');
  }
  return run('npm', args, { ...options, env: npmEnv });
}

export async function findSevenZip(env = process.env) {
  if (env.SEVEN_ZIP_PATH) return env.SEVEN_ZIP_PATH;
  const bundled = fileURLToPath(new URL(process.platform === 'win32' ? '../7za.exe' : '../7z', import.meta.url));
  try {
    await access(bundled);
    return bundled;
  } catch {
    // Fall back to a system installation when no executable was supplied in scripts/.
  }
  if (process.platform === 'win32') {
    const installed = path.join(env.ProgramFiles || 'C:\\Program Files', '7-Zip/7z.exe');
    try {
      await access(installed);
      return installed;
    } catch {
      // A portable installation can also be available on PATH.
    }
  }
  return '7z';
}
