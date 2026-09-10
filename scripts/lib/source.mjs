import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import semver from 'semver';
import { run as runProcess } from './process.mjs';

const gitNull = process.platform === 'win32' ? 'NUL' : '/dev/null';

function repositoryUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error('仓库地址必须是 HTTP(S) URL');
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      url.search || (url.hash && !/^#[A-Za-z0-9._-]+$/.test(url.hash)) ||
      /[\s\\]/.test(value)) {
    throw new Error('仓库地址必须是无凭据、查询参数的 HTTP(S) URL');
  }
  url.pathname = url.pathname.replace(/\/+$/, '');
  if (url.pathname === '/') throw new Error('仓库地址缺少路径');
  return url;
}

export async function readRepositories(file) {
  const repositories = [];
  const seen = new Set();
  const lines = (await readFile(file, 'utf8')).replace(/^\uFEFF/, '').split(/\r?\n/);
  for (const [index, raw] of lines.entries()) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    let repository;
    try {
      repository = repositoryUrl(line).href;
    } catch (error) {
      throw new Error(`repositories.txt 第 ${index + 1} 行: ${error.message}`);
    }
    const identity = new URL(repository);
    identity.pathname = identity.pathname.replace(/\.git$/i, '');
    const key = identity.hostname === 'github.com' ? identity.href.toLowerCase() : identity.href;
    if (seen.has(key)) throw new Error(`repositories.txt 第 ${index + 1} 行存在重复仓库`);
    seen.add(key);
    repositories.push(repository);
  }
  if (!repositories.length) throw new Error('repositories.txt 没有仓库地址');
  return repositories;
}

export function normalizeVersion(value) {
  const match = value.trim().match(/^[vV]?((?:0|[1-9]\d*))(?:\.((?:0|[1-9]\d*)))?(?:\.((?:0|[1-9]\d*)))?(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/);
  if (!match) throw new Error('library.properties 的 version 不是有效版本');
  const normalized = `${match[1]}.${match[2] ?? '0'}.${match[3] ?? '0'}${match[4] ?? ''}${match[5] ?? ''}`;
  if (!semver.valid(normalized)) throw new Error('library.properties 的 version 不是有效版本');
  // npm identifies versions without build metadata; keep the original source file intact.
  return semver.valid(normalized);
}

export function parseProperties(text) {
  const properties = Object.create(null);
  for (const raw of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || /^[#;]/.test(line)) continue;
    const equals = line.indexOf('=');
    if (equals < 1 || !line.slice(0, equals).trim()) {
      throw new Error('library.properties 包含无效字段');
    }
    properties[line.slice(0, equals).trim()] = line.slice(equals + 1).trim();
  }
  for (const key of ['name', 'version', 'author', 'maintainer', 'sentence']) {
    if (!properties[key]) throw new Error(`library.properties 缺少必填字段 ${key}`);
  }
  properties.version = normalizeVersion(properties.version);
  return properties;
}

function validTag(tag) {
  return tag && !tag.startsWith('-') && !/[\s\x00-\x1f\x7f~^:?*\[\\]/.test(tag) &&
    !tag.includes('..') && !tag.includes('@{') &&
    tag.split('/').every((part) => part && !part.startsWith('.') && !part.endsWith('.') && !part.endsWith('.lock'));
}

async function latestRelease(url, fetchImpl) {
  if (url.hostname !== 'github.com') return null;
  const repositoryPath = url.pathname.replace(/\.git$/i, '');
  const coordinates = repositoryPath.split('/').filter(Boolean);
  if (coordinates.length !== 2) return null;
  let response;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      response = await fetchImpl(`https://github.com${repositoryPath}/releases/latest`, {
        method: 'HEAD', redirect: 'follow', signal: AbortSignal.timeout(30000),
        headers: { 'User-Agent': 'aily-coder-libraries' },
      });
    } catch (error) {
      if (attempt === 3) {
        const code = error.cause?.code || error.code;
        const reason = error.name === 'TimeoutError' ? '请求超时'
          : ['ECONNRESET', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT',
            'UND_ERR_CONNECT_TIMEOUT', 'UND_ERR_HEADERS_TIMEOUT', 'UND_ERR_SOCKET'].includes(code) ? code : '网络请求失败';
        throw new Error(`无法确认 GitHub Latest Release（${reason}，已尝试 3 次）`);
      }
      await delay(attempt * 1000);
      continue;
    }
    if (![500, 502, 503, 504].includes(response.status) || attempt === 3) break;
    await delay(attempt * 1000);
  }
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`GitHub Latest Release 请求失败 (${response.status})`);
  const final = new URL(response.url);
  if (final.origin !== 'https://github.com' || final.username || final.password) {
    throw new Error('GitHub Latest Release 返回了非 GitHub 地址');
  }
  const parts = final.pathname.split('/').filter(Boolean);
  if (parts.length === 3 && parts[2] === 'releases') return null;
  if (parts.length < 5 || parts[2] !== 'releases' || parts[3] !== 'tag') {
    throw new Error('GitHub Latest Release 返回了无效地址');
  }
  const tag = decodeURIComponent(parts.slice(4).join('/'));
  if (!validTag(tag)) throw new Error('GitHub Latest Release 返回了无效 tag');
  return tag;
}

function gitEnvironment(base) {
  const env = { ...base };
  for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES']) {
    delete env[key];
  }
  return { ...env, GIT_CONFIG_GLOBAL: gitNull, GIT_CONFIG_NOSYSTEM: '1',
    GIT_TERMINAL_PROMPT: '0', GIT_ASKPASS: '', SSH_ASKPASS: '',
    GCM_INTERACTIVE: 'never', GIT_ALLOW_PROTOCOL: 'https:http', LC_ALL: 'C' };
}

function gitFailure(error) {
  // Only report known diagnostics and numeric protocol codes; raw output may contain proxy credentials.
  const stderr = error.stderr ?? '';
  const http = stderr.match(/(?:returned error:|HTTP(?:\/\d(?:\.\d)?)?\s+|CONNECT tunnel failed, response)\s*([45]\d{2})\b/i)?.[1];
  const curl = stderr.match(/\bcurl (\d{1,3})\b/i)?.[1];
  let reason;
  let retryable = false;
  if (error.code === 'ENOENT') {
    reason = '找不到 Git 程序或工作目录';
  } else if (error.code === 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER') {
    reason = 'Git 输出超过缓冲区上限';
  } else if (['EACCES', 'EPERM'].includes(error.code) || /permission denied|access is denied/i.test(stderr)) {
    reason = '访问权限不足';
  } else if (error.code === 'ENOSPC' || /no space left on device|disk full/i.test(stderr)) {
    reason = '本地磁盘空间不足';
  } else if (error.killed && error.signal === 'SIGTERM') {
    reason = '命令执行超时或被终止';
    retryable = true;
  } else if (http) {
    const meanings = { 401: '认证失败', 403: '访问被拒绝', 404: '仓库不存在或不可访问',
      407: '代理认证失败', 408: '请求超时', 429: '请求过于频繁' };
    reason = `HTTP ${http}（${meanings[http] ?? '远端服务错误'}）`;
    retryable = ['408', '429', '500', '502', '503', '504'].includes(http);
  } else {
    const rules = [
      [/repository not found|does not appear to be a git repository/i, '仓库不存在或不可访问', false],
      [/authentication failed|could not read (?:username|password)|terminal prompts disabled/i, '认证失败或无访问权限', false],
      [/couldn't find remote ref|not our ref|unadvertised object/i, '远端 tag 或对象不存在', false],
      [/SSL certificate problem|certificate verify failed|server certificate verification failed/i, 'TLS 证书校验失败', false],
      [/could not resolve proxy/i, '无法解析代理地址', true],
      [/could not resolve host/i, '无法解析仓库域名', true],
      [/failed to connect|could not connect|connection refused/i, '无法连接远端或代理', true],
      [/timed? out|timeout was reached|operation too slow/i, '网络请求超时', true],
      [/connection (?:was )?reset|recv failure|send failure|early EOF|unexpected disconnect|remote end hung up|empty reply/i, '连接中断或传输不完整', true],
      [/HTTP\/2.*(?:error|closed|reset)|SSL connect error|SSL_ERROR_SYSCALL|TLS connection was non-properly terminated|failed to receive handshake/i, 'HTTP/2 或 TLS 连接中断', true],
    ];
    const match = rules.find(([pattern]) => pattern.test(stderr));
    reason = match?.[1] ?? '未识别的 Git 错误（原始输出已隐藏）';
    retryable = match ? match[2] : ['5', '6', '7', '16', '18', '28', '35', '52', '55', '56', '92'].includes(curl);
    if (!match && retryable) reason = '网络传输失败';
  }
  if (curl) reason += `（curl ${curl}）`;
  const status = Number.isInteger(error.code) ? `退出码 ${error.code}`
    : ['ENOENT', 'EACCES', 'EPERM', 'ENOSPC', 'ERR_CHILD_PROCESS_STDIO_MAXBUFFER'].includes(error.code) ? `错误码 ${error.code}`
      : /^SIG[A-Z]+$/.test(error.signal ?? '') ? `终止信号 ${error.signal}` : '退出状态未知';
  return { reason, retryable, status };
}

function safeTree(output) {
  let hasProperties = false;
  const paths = new Map();
  for (const entry of output.split('\0').filter(Boolean)) {
    const tab = entry.indexOf('\t');
    const [mode, type] = entry.slice(0, tab).split(' ');
    const name = entry.slice(tab + 1);
    if (mode === '120000') throw new Error('源码包含 symlink');
    if (mode === '160000' || type === 'commit') throw new Error('源码包含 submodule');
    if (tab < 0 || type !== 'blob' || !['100644', '100755'].includes(mode)) {
      throw new Error('源码包含不支持的 Git tree 条目');
    }
    const parts = name.split('/');
    if (/[\\:\x00-\x1f\x7f]/.test(name) || parts.some((part) =>
      !part || /[. ]$/.test(part) || part.toLowerCase() === '.git')) {
      throw new Error('源码包含不安全的归档路径');
    }
    for (let index = 0; index < parts.length; index++) {
      const prefix = parts.slice(0, index + 1).join('/');
      const key = prefix.toLowerCase();
      const kind = index === parts.length - 1 ? 'file' : 'directory';
      const existing = paths.get(key);
      if (existing && (existing.name !== prefix || existing.kind !== kind)) {
        throw new Error('源码包含不安全的归档路径：大小写或文件目录冲突');
      }
      paths.set(key, { name: prefix, kind });
    }
    if (name === 'library.properties') hasProperties = true;
  }
  if (!hasProperties) throw new Error('源码根目录缺少 library.properties');
}

export async function downloadSource(repository, workDirectory, { run = runProcess, fetchImpl = fetch, env = process.env } = {}) {
  const url = repositoryUrl(repository);
  url.hash = '';
  let preferredTag;
  try {
    preferredTag = await latestRelease(url, fetchImpl);
  } catch {
    console.warn(`${url.href}: 无法确认 GitHub Latest Release，改为按普通 tag 选择最新有效版本。`);
  }
  const gitDirectory = path.resolve(workDirectory, 'repository.git');
  const archivePath = path.resolve(workDirectory, 'source.zip');
  await mkdir(workDirectory, { recursive: true });
  const git = async (args, tag) => {
    const command = args[0] === '--git-dir' ? args[2] : args[0];
    const attempts = ['ls-remote', 'fetch'].includes(command) ? 3 : 1;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        return await run('git', ['-c', `core.hooksPath=${gitNull}`, '-c', 'credential.helper=',
          '-c', 'core.askPass=', '-c', 'core.fsmonitor=false', ...args], { cwd: workDirectory, env: gitEnvironment(env) });
      } catch (error) {
        const { reason, retryable, status } = gitFailure(error);
        const message = `git ${command} 失败${tag ? `（tag=${tag}）` : ''}：${reason}（${status}，已尝试 ${attempt} 次）`;
        if (!retryable || attempt === attempts) throw new Error(message);
        console.warn(`${url.href}: ${message}；${attempt} 秒后重试。`);
        await delay(attempt * 1000);
      }
    }
  };
  const remoteArgs = ['ls-remote', '--tags', url.href];
  if (preferredTag) remoteArgs.push(`refs/tags/${preferredTag}`, `refs/tags/${preferredTag}^{}`);
  const advertised = (await git(remoteArgs)).stdout;
  const tags = new Map();
  for (const line of advertised.trim().split(/\r?\n/).filter(Boolean)) {
    const match = line.match(/^([0-9a-f]{40}|[0-9a-f]{64})\s+refs\/tags\/(.+)$/);
    if (!match) throw new Error('git ls-remote 返回了无效 tag');
    if (match[2].endsWith('^{}')) continue;
    if (!validTag(match[2]) || (preferredTag && match[2] !== preferredTag)) {
      throw new Error('git ls-remote 返回了未请求或无效 tag');
    }
    tags.set(match[2], match[1]);
  }
  if (!tags.size) throw new Error('仓库没有可用 tag');
  if (tags.size > 1000) throw new Error('单仓库 tag 数超过 1000 上限');
  await git(['init', '--bare', '--quiet', '--template=', gitDirectory]);
  const local = (args, tag) => git(['--git-dir', gitDirectory, ...args], tag);
  let selected;
  for (const [tag, oid] of tags) {
    await local(['fetch', '--quiet', '--depth=1', '--no-tags', '--no-recurse-submodules',
      url.href, `+refs/tags/${tag}:refs/tags/aily-coder-source`], tag);
    const fetched = (await local(['rev-parse', '--verify', 'refs/tags/aily-coder-source'])).stdout.trim();
    if (fetched !== oid) throw new Error('tag 在发现与下载之间发生变化，请重试');
    let commit;
    let properties;
    try {
      commit = (await local(['rev-parse', '--verify', 'refs/tags/aily-coder-source^{commit}'])).stdout.trim();
      if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(commit)) throw new Error('tag 没有有效 commit');
      properties = parseProperties((await local(['show', `${commit}:library.properties`])).stdout);
      if (!selected || semver.gt(properties.version, selected.properties.version)) {
        safeTree((await local(['ls-tree', '-r', '-z', '--full-tree', commit])).stdout);
        selected = { properties, tag, commit, archivePath };
      }
    } catch (error) {
      if (preferredTag) throw error;
      continue;
    }
  }
  if (!selected) throw new Error('所有 tag 均缺少有效的 library.properties 或源码结构');
  // Upstream export-ignore/export-subst rules must not omit or rewrite the source.
  await mkdir(path.join(gitDirectory, 'info'), { recursive: true });
  await writeFile(path.join(gitDirectory, 'info', 'attributes'), '* -export-ignore -export-subst\n');
  await local(['archive', '--format=zip', `--output=${archivePath}`, selected.commit]);
  return selected;
}
