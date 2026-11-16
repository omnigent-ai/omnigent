// Per-session Files tree navigation (browsed folder, user-expanded folders per
// location) kept in localStorage so a reload lands where the user left off.
// Every level and the total size are capped so it can't crowd out other storage.

const STORAGE_KEY = "omnigent:files-panel-tree-state";
// Sessions kept, least recently changed pruned first.
const MAX_SESSIONS = 30;
// Browsed locations kept per session, least recently changed pruned first.
const MAX_LOCATIONS_PER_SESSION = 10;
// Expanded folders kept per location; the shallowest win so restored folders stay reachable.
export const MAX_EXPANDED_PATHS = 300;
// Serialized size budget; the least recently changed sessions go first to stay within it.
export const MAX_STORE_CHARS = 200_000;

interface ExpandedEntry {
  location: string;
  paths: string[];
}

interface TreeState {
  /** Absolute browsed folder; absent while browsing the workspace root. */
  browseLocation?: string;
  /** Expanded folders per browsed location, least recently changed first. */
  expanded: ExpandedEntry[];
}

/** Ordered array, least recently changed first, so order survives serialization. */
type Store = { id: string; state: TreeState }[];

// Sessions changed while localStorage refused writes (quota, privacy mode), laid
// over the stored ones so this page keeps its navigation until a write succeeds.
const unsavedSessions = new Map<string, TreeState>();

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.filter((item): item is string => typeof item === "string");
}

function sanitizeState(value: unknown): TreeState {
  const state: TreeState = { expanded: [] };
  if (typeof value !== "object" || value === null) return state;
  const record = value as Record<string, unknown>;
  if (typeof record.browseLocation === "string" && record.browseLocation !== "") {
    state.browseLocation = record.browseLocation;
  }
  if (Array.isArray(record.expanded)) {
    for (const item of record.expanded) {
      if (typeof item !== "object" || item === null) continue;
      const entry = item as Record<string, unknown>;
      const paths = stringArray(entry.paths);
      if (typeof entry.location !== "string" || !paths) continue;
      state.expanded.push({ location: entry.location, paths });
    }
  }
  return state;
}

function parseStore(raw: string | null): Store {
  try {
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const store: Store = [];
    for (const item of parsed) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.id !== "string") continue;
      store.push({ id: record.id, state: sanitizeState(record.state) });
    }
    return store;
  } catch {
    return [];
  }
}

function readPersistedStore(): Store {
  if (typeof window === "undefined") return [];
  try {
    return parseStore(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return [];
  }
}

function sessionSnapshot(store: Store, id: string): string | undefined {
  const entry = store.find((item) => item.id === id);
  return entry && JSON.stringify(entry.state);
}

// Another tab's newer save of a session replaces this page's unsaved copy of it.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (unsavedSessions.size === 0 || event.key !== STORAGE_KEY) return;
    try {
      if (event.storageArea !== null && event.storageArea !== window.localStorage) return;
    } catch {
      return;
    }
    const before = parseStore(event.oldValue);
    const after = parseStore(event.newValue);
    for (const id of [...unsavedSessions.keys()]) {
      if (sessionSnapshot(before, id) !== sessionSnapshot(after, id)) unsavedSessions.delete(id);
    }
  });
}

function readStore(): Store {
  const store = readPersistedStore().filter((entry) => !unsavedSessions.has(entry.id));
  for (const [id, state] of unsavedSessions) store.push({ id, state: structuredClone(state) });
  return store;
}

/** Serialize `store`, dropping the least recently changed entries to fit the budget. */
function serializeWithinBudget(store: Store): string {
  let json = JSON.stringify(store);
  while (json.length > MAX_STORE_CHARS && store.length > 0) {
    const newest = store[store.length - 1];
    const newestFits = JSON.stringify([newest]).length <= MAX_STORE_CHARS;
    if (newestFits && store.length > 1) store.shift();
    // A session too big on its own loses its oldest locations, which fall back to defaults.
    else if (newest.state.expanded.length > 0) newest.state.expanded.shift();
    else store.splice(0);
    json = JSON.stringify(store);
  }
  return json;
}

function writeStore(store: Store, changedId: string, changedState: TreeState): void {
  if (typeof window === "undefined") return;
  const changedSnapshot = structuredClone(changedState);
  try {
    window.localStorage.setItem(STORAGE_KEY, serializeWithinBudget(store));
    unsavedSessions.clear();
  } catch {
    unsavedSessions.delete(changedId);
    unsavedSessions.set(changedId, changedSnapshot);
    if (unsavedSessions.size > MAX_SESSIONS) {
      unsavedSessions.delete(unsavedSessions.keys().next().value!);
    }
  }
}

/** Apply `update` to one session's state and move it to the most-recent end. */
function updateSession(conversationId: string, update: (state: TreeState) => void): void {
  const store = readStore();
  const index = store.findIndex((entry) => entry.id === conversationId);
  const state = index >= 0 ? store[index].state : { expanded: [] };
  if (index >= 0) store.splice(index, 1);
  update(state);
  store.push({ id: conversationId, state });
  if (store.length > MAX_SESSIONS) store.splice(0, store.length - MAX_SESSIONS);
  writeStore(store, conversationId, state);
}

function depth(path: string): number {
  return path.split("/").length;
}

/** The folder a session was last browsing, or null for its workspace root. */
export function readFilesTreeBrowseLocation(conversationId: string): string | null {
  const entry = readStore().find((item) => item.id === conversationId);
  return entry?.state.browseLocation ?? null;
}

/** Remember the folder a session is browsing; null returns it to the workspace root. */
export function writeFilesTreeBrowseLocation(
  conversationId: string,
  browseLocation: string | null,
): void {
  updateSession(conversationId, (state) => {
    if (browseLocation) state.browseLocation = browseLocation;
    else delete state.browseLocation;
  });
}

/** Folders the user left expanded at a location; undefined means use the tree's defaults. */
export function readFilesTreeExpandedPaths(
  conversationId: string,
  location: string,
): string[] | undefined {
  const entry = readStore().find((item) => item.id === conversationId);
  return entry?.state.expanded.find((item) => item.location === location)?.paths;
}

/** Remember the folders expanded at one browsed location. */
export function writeFilesTreeExpandedPaths(
  conversationId: string,
  location: string,
  paths: Iterable<string>,
): void {
  let kept = [...new Set(paths)];
  if (kept.length > MAX_EXPANDED_PATHS) {
    kept = kept
      .map((path, order) => ({ path, order }))
      .sort((a, b) => depth(a.path) - depth(b.path) || a.order - b.order)
      .slice(0, MAX_EXPANDED_PATHS)
      .map(({ path }) => path);
  }
  updateSession(conversationId, (state) => {
    state.expanded = state.expanded.filter((item) => item.location !== location);
    state.expanded.push({ location, paths: kept });
    if (state.expanded.length > MAX_LOCATIONS_PER_SESSION) {
      state.expanded.splice(0, state.expanded.length - MAX_LOCATIONS_PER_SESSION);
    }
  });
}
