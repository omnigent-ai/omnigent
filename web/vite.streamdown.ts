// Streamdown mounts mermaid diagrams and highlighted code bodies behind
// `React.lazy(() => import('./<facade>-<hash>.js'))` — tiny dist facades that
// only re-export components its static chunk already carries. Left alone, the
// bundler emits each facade as its own hashed chunk fetched at first render: a
// tab held across a redeploy 404s that fetch, `React.lazy` caches the rejection
// forever, and every later diagram degrades to "Could not render this
// markdown." until a full reload. Grouping the whole dist into one chunk keeps
// those dynamic imports in-chunk (the code arrives with the statically-loaded
// bundle), so rendering markdown never fetches a chunk that could fail.
export function streamdownManualChunk(id: string): string | undefined {
  const normalized = id.replaceAll("\\", "/");
  if (normalized.includes("/node_modules/streamdown/dist/")) return "streamdown";
  return undefined;
}
