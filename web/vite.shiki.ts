// Keep Shiki core and engines together while preserving lazy language chunks.
// Splitting the core exposes an initialization cycle in pnpm-linked builds.
export function shikiManualChunk(id: string): string | undefined {
  const normalized = id.replaceAll("\\", "/");
  if (normalized.includes("/@shikijs/langs/")) return undefined;
  if (normalized.includes("/shiki") || normalized.includes("/@shikijs/")) return "shiki";
  return undefined;
}
