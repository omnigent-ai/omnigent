// Persisted, app-global "remembered options" for the new-session landing
// composer, keyed by harness. Each harness gets a small string->string map of
// the option knobs the user last picked for it — Claude Code's permission mode
// + model + effort, Codex's / OpenCode's approval mode, Cursor's exec mode — so
// a returning user's new session seeds those instead of starting on the harness
// default.
//
// One store holding a per-harness options OBJECT (not a single value), so any
// harness with start-session options remembers all of them under one roof. The
// landing screen keeps live React state as the source of truth; these helpers
// only snapshot a pick and seed it back later. The consumer validates each
// value against the harness's CURRENT option vocabulary and falls back to
// unselected when a stored value no longer exists — so changing the selectable
// options can never post a dead value.
//
// The localStorage key is the historical `last-mode-by-harness` (from when this
// only held the single mode string). We keep it and migrate the legacy shape on
// read — a bare string value `v` becomes `{ mode: v }` — so a returning user's
// remembered mode survives the generalization without a reset.

const STORAGE_KEY = "omnigent:last-mode-by-harness";

/** A harness's remembered option knobs (e.g. `{ mode, model, effort }`). */
export type HarnessOptions = Record<string, string>;

type OptionsMap = Record<string, HarnessOptions>;

/**
 * Coerce one stored entry into a clean string->string options map. Returns an
 * empty map for anything that can't be salvaged (a corrupt shape — the caller
 * then falls back to unselected). The legacy single-string form (this store's
 * original schema) becomes `{ mode: <value> }`; objects keep only their
 * string-valued fields, so a future field whose format drifted is dropped
 * rather than mis-seeded.
 */
function coerceEntry(value: unknown): HarnessOptions {
  if (typeof value === "string") return { mode: value };
  if (value === null || typeof value !== "object" || Array.isArray(value)) return {};
  const out: HarnessOptions = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (typeof v === "string") out[k] = v;
  }
  return out;
}

function readMap(): OptionsMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: OptionsMap = {};
    for (const [harness, value] of Object.entries(parsed as Record<string, unknown>)) {
      const entry = coerceEntry(value);
      if (Object.keys(entry).length > 0) out[harness] = entry;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Read the remembered options for `harness`. Returns an empty object when
 * nothing is stored, on a server render (no `window`), or when storage is
 * inaccessible/corrupted — never throws. Legacy bare-string entries are
 * migrated to `{ mode }` transparently.
 */
export function readHarnessOptions(harness: string | null | undefined): HarnessOptions {
  if (!harness) return {};
  return readMap()[harness] ?? {};
}

/**
 * Merge `patch` into `harness`'s remembered options (a partial set of knobs, so
 * a model-only change preserves a stored effort/mode). Reading through `readMap`
 * first also normalizes/self-heals any legacy or corrupt entries on write.
 * Swallows quota/access errors so a failed write can't break session creation.
 */
export function writeHarnessOption(
  harness: string | null | undefined,
  patch: HarnessOptions,
): void {
  if (typeof window === "undefined" || !harness) return;
  try {
    const map = readMap();
    map[harness] = { ...map[harness], ...patch };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // localStorage quota or access errors shouldn't break the composer.
  }
}
