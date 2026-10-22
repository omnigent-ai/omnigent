const RELOAD_KEY = "omnigent:chunk-reload-at";
const RELOAD_COOLDOWN_MS = 60_000;

/** Browser/Vite and webpack/rspack errors for missing lazy JS or CSS chunks. */
export function isChunkLoadError(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  if ("name" in error && error.name === "ChunkLoadError") return true;
  if ("code" in error && error.code === "CSS_CHUNK_LOAD_FAILED") return true;
  if (!("message" in error) || typeof error.message !== "string") return false;
  return /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS for/i.test(
    error.message,
  );
}

/** Keep the URL intact and retain the cooldown across reloads, not just renders. */
export function reloadAfterChunkError(): boolean {
  if (!navigator.onLine) return false;
  try {
    const lastReload = Number(sessionStorage.getItem(RELOAD_KEY));
    const now = Date.now();
    if (lastReload && now - lastReload < RELOAD_COOLDOWN_MS) return false;
    sessionStorage.setItem(RELOAD_KEY, String(now));
  } catch {
    // Without a persistent guard, automatic recovery could loop indefinitely.
    return false;
  }
  location.reload();
  return true;
}
