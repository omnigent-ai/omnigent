/**
 * Recovery for lazy route chunks that a redeploy deleted out from under an
 * open tab.
 *
 * Route pages load through hashed chunk URLs baked into the bundle the tab
 * booted with. A deploy that replaces those assets removes the old hashes, so
 * the tab's next navigation rejects its dynamic import — and with no recovery
 * a rejected `React.lazy` unmounts the router into a permanently blank page.
 * The recovery contract:
 *
 *  - First failure: reload the page once. The fresh index.html references the
 *    new chunk graph, so the interrupted navigation succeeds after the reload.
 *  - Failure right after a recovery reload (broken asset graph, offline tab):
 *    throw {@link LazyChunkLoadError} into `RouteChunkErrorBoundary`, which
 *    renders an explicit reload affordance instead of a blank page — never
 *    loop reloads.
 *
 * The once-guard lives in sessionStorage so it survives the reload itself but
 * stays scoped to the tab. When the guard cannot be persisted, the auto reload
 * is skipped entirely (an unguarded reload could loop forever) and the
 * boundary affordance is the recovery.
 */

/** sessionStorage key recording when the last recovery reload was issued. */
export const CHUNK_RELOAD_AT_STORAGE_KEY = "omnigent:lazy-chunk-reload-at";

/** A second import failure inside this window means the reload didn't help. */
const RELOAD_LOOP_WINDOW_MS = 60_000;

/** Injectable seams so tests never touch the real page or clock. */
export interface ChunkRecoveryEnv {
  now: () => number;
  reload: () => void;
  storage: () => Storage | null;
}

const defaultEnv: ChunkRecoveryEnv = {
  now: () => Date.now(),
  reload: () => window.location.reload(),
  storage: () => {
    try {
      // Can throw in privacy modes / storage-blocked sandboxed iframes.
      return window.sessionStorage;
    } catch {
      return null;
    }
  },
};

/**
 * Envs that already issued a recovery reload in this document lifetime. A
 * route can suspend on several chunks at once; once one of them triggered the
 * reload, sibling failures should hold (the page is tearing down) rather than
 * flash the error boundary.
 */
const reloadRequested = new WeakMap<ChunkRecoveryEnv, boolean>();

/** Thrown into the route error boundary when auto-reload is spent or unavailable. */
export class LazyChunkLoadError extends Error {
  constructor(cause: unknown) {
    super("A lazily loaded page chunk could not be loaded", { cause });
    this.name = "LazyChunkLoadError";
  }
}

function readReloadAt(storage: Storage | null): number | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(CHUNK_RELOAD_AT_STORAGE_KEY);
    if (raw === null) return null;
    const at = Number(raw);
    return Number.isFinite(at) ? at : null;
  } catch {
    return null;
  }
}

/** Returns false when the guard cannot be persisted (reload could loop). */
function armReloadGuard(storage: Storage | null, at: number): boolean {
  if (!storage) return false;
  try {
    storage.setItem(CHUNK_RELOAD_AT_STORAGE_KEY, String(at));
    return true;
  } catch {
    return false;
  }
}

/** Re-arm auto-recovery; a user-invoked reload always starts from a clean guard. */
export function clearChunkReloadGuard(env: ChunkRecoveryEnv = defaultEnv): void {
  try {
    env.storage()?.removeItem(CHUNK_RELOAD_AT_STORAGE_KEY);
  } catch {
    // Nothing to clear when storage is unavailable.
  }
}

/**
 * Wrap a lazy route's import so a chunk deleted by a redeploy recovers by
 * reloading once instead of blanking the app. Compose under `React.lazy`:
 * `lazy(reloadOnMissingChunk(() => import("@/pages/FooPage").then(…)))`.
 */
export function reloadOnMissingChunk<T>(
  load: () => Promise<T>,
  env: ChunkRecoveryEnv = defaultEnv,
): () => Promise<T> {
  return async () => {
    try {
      return await load();
    } catch (error) {
      // Hold the Suspense fallback while a reload is already tearing the page
      // down — resolving or rethrowing would flash an error mid-teardown.
      const pending = new Promise<never>(() => {});
      if (reloadRequested.get(env)) return pending;

      const now = env.now();
      const reloadedAt = readReloadAt(env.storage());
      const justReloaded = reloadedAt !== null && now - reloadedAt < RELOAD_LOOP_WINDOW_MS;
      if (!justReloaded && armReloadGuard(env.storage(), now)) {
        reloadRequested.set(env, true);
        env.reload();
        return pending;
      }
      throw new LazyChunkLoadError(error);
    }
  };
}
