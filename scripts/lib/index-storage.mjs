import { readFile } from 'node:fs/promises';
import { PutObjectCommand, S3Client } from '@aws-sdk/client-s3';

function required(env, name, alias) {
  const value = env[name]?.trim() || (alias && env[alias]?.trim());
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function endpoint(value, name) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)
      || url.username || url.password || url.search || url.hash) throw new Error();
    return url.href.replace(/\/+$/, '');
  } catch {
    throw new Error(`${name} must be an HTTP(S) endpoint without credentials, query, or fragment`);
  }
}

export function loadIndexTargets(env = process.env) {
  const rustfs = {
    name: 'RustFS',
    endpoint: endpoint(required(env, 'RUSTFS_ENDPOINT'), 'RUSTFS_ENDPOINT'),
    region: env.RUSTFS_REGION?.trim() || 'us-east-1',
    credentials: {
      accessKeyId: required(env, 'RUSTFS_ACCESS_KEY_ID', 'RUSTFS_ACCESS_KEY'),
      secretAccessKey: required(env, 'RUSTFS_SECRET_ACCESS_KEY', 'RUSTFS_SECRET_KEY'),
    },
  };
  const r2Endpoint = env.R2_ENDPOINT?.trim()
    || `https://${required(env, 'R2_ACCOUNT_ID')}.r2.cloudflarestorage.com`;
  return [rustfs, {
    name: 'R2',
    endpoint: endpoint(r2Endpoint, 'R2_ENDPOINT'),
    region: env.R2_REGION?.trim() || 'auto',
    credentials: {
      accessKeyId: required(env, 'R2_ACCESS_KEY_ID'),
      secretAccessKey: required(env, 'R2_SECRET_ACCESS_KEY'),
    },
  }];
}

export async function uploadIndex(file, targets) {
  const body = await readFile(file);
  const failed = [];
  for (const target of targets) {
    const client = new S3Client({
      endpoint: target.endpoint,
      region: target.region,
      credentials: target.credentials,
      forcePathStyle: true,
      requestHandler: { connectionTimeout: 10_000, requestTimeout: 120_000 },
    });
    try {
      await client.send(new PutObjectCommand({
        Bucket: 'ailyblockly',
        Key: 'libraries-coder-index.json',
        Body: body,
        ContentType: 'application/json',
        CacheControl: 'no-store, no-cache, must-revalidate, max-age=0',
      }));
    } catch {
      failed.push(target.name);
    } finally {
      client.destroy();
    }
  }
  if (failed.length) throw new Error(`Index upload failed for ${failed.join(', ')}; rerun to retry`);
}
