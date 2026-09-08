import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { uploadIndex } from './index-storage.mjs';

export const INDEX_FILENAME = 'libraries-coder-index.json';

export function createLibraryEntry(manifest, properties) {
  const architectures = properties.architectures?.split(',').map(value => value.trim()).filter(Boolean) || [];
  const entry = {
    name: manifest.name,
    nickname: properties.name,
    version: manifest.version,
    description: manifest.description,
    author: manifest.author,
    keywords: manifest.keywords,
    repository: manifest.repository,
    category: properties.category || 'Uncategorized',
    architectures: architectures.length ? architectures : ['*'],
  };
  for (const key of ['homepage', 'license']) {
    if (manifest[key]) entry[key] = manifest[key];
  }
  if (properties.includes) {
    entry.providesIncludes = properties.includes.split(',').map(value => value.trim()).filter(Boolean);
  }
  return entry;
}

export async function syncLibraryIndex(entries, outputDirectory, { dryRun, partial, targets }) {
  const libraries = [...entries].sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
  const file = path.join(outputDirectory, INDEX_FILENAME);
  await writeFile(file, JSON.stringify({ libraries }));
  // Dry runs and custom repository lists leave the remote index unchanged.
  const uploaded = !dryRun && !partial;
  if (uploaded) await uploadIndex(file, targets);
  return { file, uploaded };
}
