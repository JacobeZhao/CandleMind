const readers = new Map();

export function registerRefreshReader(key, reader) {
  readers.set(key, reader);
  return () => {
    if (readers.get(key) === reader) readers.delete(key);
  };
}

export async function refreshMountedReaders() {
  const entries = [...readers.entries()];
  const results = await Promise.allSettled(entries.map(([, reader]) => reader()));
  return results.map((result, index) => ({ key: entries[index][0], ...result }));
}
