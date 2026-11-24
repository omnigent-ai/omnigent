// Keep Shiki core and engines together while preserving lazy language chunks.
// Splitting the core exposes an initialization cycle in pnpm-linked builds.
// Only installed packages match: the chunk loads with the app, so an app module named
// after Shiki would load with it instead of with the code that uses it.
export function shikiManualChunk(id: string): string | undefined {
  const normalized = id.replaceAll("\\", "/");
  if (!normalized.includes("/node_modules/")) return undefined;
  if (normalized.includes("/@shikijs/langs/")) return undefined;
  if (normalized.includes("/shiki") || normalized.includes("/@shikijs/")) return "shiki";
  return undefined;
}
