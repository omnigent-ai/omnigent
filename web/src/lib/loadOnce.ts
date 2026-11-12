/** A loader that runs `load` on its first call and returns the same promise
 *  after, unless it failed: then the next call runs `load` again. For lazy
 *  `import()`s whose chunk can fail to download. */
export function loadOnce<T>(load: () => Promise<T>): () => Promise<T> {
  let loading: Promise<T> | undefined;
  return () =>
    (loading ??= load().catch((err: unknown) => {
      loading = undefined;
      throw err;
    }));
}
