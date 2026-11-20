// The outbox's unsent design requests in IndexedDB, so a reload or crash doesn't lose them.
// Each names its server, user and browser tab: a tab takes back its own, and another tab's
// only once Web Locks show that tab is gone (adopting under one lock across windows).
import { getBasePath } from "@/lib/basePath";
import type { DesignRequest, DesignRequestStatus, DesignTool } from "./designOutbox";
import { DESIGN_OUTBOX_DATABASE, noteSavedDesignRequests } from "./designOutboxPresence";

const STORE = "requests";
const TAB_KEY = "omnigent.designOutbox.tab";
const TAB_LOCK_PREFIX = "omnigent-design-outbox-page:";
const ADOPT_LOCK = "omnigent-design-outbox-adopt:";

interface SavedFile {
  name: string;
  type: string;
  lastModified: number;
  bytes: ArrayBuffer;
}

export interface SavedRequest {
  id: string;
  tabId: string;
  conversationId: string;
  label: string;
  tool?: DesignTool;
  text: string;
  files: SavedFile[];
  status: Exclude<DesignRequestStatus, "sent">;
  error?: string;
  /** Its last attempt was refused (it never reached the agent). */
  refused?: true;
  /** When it was first saved: later saves keep it, so its age runs from when it was made. */
  savedAt: number;
  /** The browser tab that owns it (see `thisTab`). */
  page: string;
  /** The signed-in user who made it. */
  userId: string;
  /** The server it's for (see `thisServer`). */
  server: string;
}

const newTabId = () => Math.random().toString(36).slice(2, 12);

function storedTabId(): string {
  try {
    let id = sessionStorage.getItem(TAB_KEY);
    if (!id) {
      id = newTabId();
      sessionStorage.setItem(TAB_KEY, id);
    }
    return id;
  } catch {
    return newTabId();
  }
}

let tab = storedTabId();

/** This browser tab's id: the same across its reloads. */
export function thisTab(): string {
  return tab;
}

/** The server this page talks to: requests saved for another one on this origin stay its. */
export function thisServer(): string {
  return `${globalThis.location?.origin ?? ""}${getBasePath()}`;
}

const hasLocks = () => typeof navigator !== "undefined" && !!navigator.locks?.query;
let tabLock: Promise<void> | null = null;
// Page locks are per server, so a tab now on another server doesn't keep its requests here.
const tabLockPrefix = () => `${TAB_LOCK_PREFIX}${thisServer()}#`;

/**
 * Hold this tab's lock for as long as it's open (the browser drops it when the tab goes).
 * A duplicated tab copies sessionStorage, and with it the id; finding that id's lock held,
 * it takes a new id.
 */
function takeTabLock(): Promise<void> {
  if (!hasLocks()) return Promise.resolve();
  tabLock ??= (async () => {
    for (;;) {
      const name = tabLockPrefix() + tab;
      // oxlint-disable-next-line no-await-in-loop -- try one id at a time
      const taken = await new Promise<boolean>((settled) => {
        navigator.locks
          .request(name, { ifAvailable: true }, (lock) => {
            settled(lock !== null);
            return lock ? new Promise<never>(() => {}) : undefined;
          })
          // Locks refused here (an opaque origin, say): keep this id, as without Web Locks.
          .catch(() => settled(true));
      });
      if (taken) return;
      tab = newTabId();
      try {
        sessionStorage.setItem(TAB_KEY, tab);
      } catch {
        // The new id lasts as long as this page.
      }
    }
  })();
  return tabLock;
}

/** Run `task` while no other window adopts (one at a time across windows). */
async function exclusively<T>(task: () => Promise<T>): Promise<T> {
  if (!hasLocks()) return task();
  let ran = false;
  try {
    return (await navigator.locks.request(ADOPT_LOCK + thisServer(), () => {
      ran = true;
      return task();
    })) as T;
  } catch (error) {
    // The lock refused, not the task failing: run it as without Web Locks.
    if (ran) throw error;
    return task();
  }
}

/** Tabs open on this server now (they hold their lock). */
async function openTabs(): Promise<Set<string>> {
  const prefix = tabLockPrefix();
  const { held = [] } = await navigator.locks.query();
  return new Set(
    held
      .map((lock) => lock.name ?? "")
      .filter((name) => name.startsWith(prefix))
      .map((name) => name.slice(prefix.length)),
  );
}

/** Resolves once `transaction` commits; rejects if it fails or aborts (a full disk, say). */
function committed(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB aborted"));
  });
}

let opening: Promise<IDBDatabase> | null = null;

function database(): Promise<IDBDatabase> {
  if (!globalThis.indexedDB) return Promise.reject(new Error("IndexedDB is unavailable"));
  opening ??= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DESIGN_OUTBOX_DATABASE, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Couldn't open the design outbox"));
  }).catch((error: unknown) => {
    opening = null;
    throw error;
  });
  return opening;
}

async function store(mode: IDBTransactionMode): Promise<IDBObjectStore> {
  return (await database()).transaction(STORE, mode).objectStore(STORE);
}

/**
 * Note whether any are saved, in the count's own transaction: another tab's save can't
 * commit between the count and the note, so a stale "none" can't overwrite its "some".
 */
async function noteCount(): Promise<void> {
  const objects = await store("readonly");
  const counting = objects.count();
  counting.onsuccess = () => noteSavedDesignRequests(counting.result > 0);
  await committed(objects.transaction);
}

async function bytesOf(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("Couldn't read the file"));
    reader.readAsArrayBuffer(file);
  });
}

// A request's text and files never change, so its files are read once (once it works).
const savedFiles = new Map<string, Promise<SavedFile[]>>();
// When each request this page saves or adopts was first saved.
const firstSaved = new Map<string, number>();

function filesOf(request: DesignRequest): Promise<SavedFile[]> {
  const cached = savedFiles.get(request.id);
  if (cached) return cached;
  const files = Promise.all(
    request.files.map(async (file) => ({
      name: file.name,
      type: file.type,
      lastModified: file.lastModified,
      bytes: await bytesOf(file),
    })),
  );
  savedFiles.set(request.id, files);
  // A failed read isn't kept, so the next save reads again.
  files.catch(() => {
    if (savedFiles.get(request.id) === files) savedFiles.delete(request.id);
  });
  return files;
}

/** Save `request` (not sent) as this tab's, made by `userId`; resolves once it's committed. */
export async function saveRequest(request: DesignRequest, userId: string): Promise<void> {
  if (request.status === "sent") return deleteRequest(request.id);
  await takeTabLock();
  const record: SavedRequest = {
    id: request.id,
    tabId: request.tabId,
    conversationId: request.conversationId,
    label: request.label,
    ...(request.tool ? { tool: request.tool } : {}),
    text: request.text,
    files: await filesOf(request),
    status: request.status,
    ...(request.error ? { error: request.error } : {}),
    ...(request.delivery === "refused" ? { refused: true as const } : {}),
    savedAt: firstSaved.get(request.id) ?? Date.now(),
    page: tab,
    userId,
    server: thisServer(),
  };
  const objects = await store("readwrite");
  objects.put(record);
  await committed(objects.transaction);
  if (!firstSaved.has(record.id)) firstSaved.set(record.id, record.savedAt);
  noteSavedDesignRequests(true);
}

/** Forget a saved request (sent, or removed); resolves once it's committed. */
export async function deleteRequest(id: string): Promise<void> {
  savedFiles.delete(id);
  firstSaved.delete(id);
  const objects = await store("readwrite");
  objects.delete(id);
  await committed(objects.transaction);
  await noteCount();
}

/**
 * How old a saved request may be when a page restores: older ones, anyone's, are deleted then.
 * It's a restore-time cleanup: a request a window still holds stays until that window lets go.
 */
export const SAVED_REQUEST_TTL_MS = 24 * 60 * 60_000;

/**
 * The saved requests of `userId` on this server this tab may take back: its own, and those
 * of tabs now closed, except those of `held` (this page's own, still live). Another user's
 * for this server, and anyone's past SAVED_REQUEST_TTL_MS, are deleted: `userId` is the
 * server's answer to who is signed in, so they aren't left on a shared machine.
 */
export function adoptSavedRequests(
  userId: string,
  held: ReadonlySet<string> = new Set(),
): Promise<SavedRequest[]> {
  return exclusively(async () => {
    await takeTabLock();
    const open = hasLocks() ? await openTabs() : null;
    const free = (owner: string) => owner === tab || (open !== null && !open.has(owner));
    const server = thisServer();
    const objects = await store("readwrite");
    const adopted: SavedRequest[] = [];
    const reading = objects.getAll();
    // Claimed in the read's own callback, so it's the same transaction.
    reading.onsuccess = () => {
      const all = reading.result as SavedRequest[];
      const expired = Date.now() - SAVED_REQUEST_TTL_MS;
      for (const record of all.sort((a, b) => a.savedAt - b.savedAt)) {
        const mineHeld = record.page === tab && held.has(record.id);
        const othersHere = record.server === server && record.userId !== userId;
        if (!mineHeld && (record.savedAt < expired || othersHere)) {
          objects.delete(record.id);
          continue;
        }
        if (record.userId !== userId || record.server !== server || !free(record.page)) continue;
        if (record.page === tab && held.has(record.id)) continue;
        const mine = { ...record, page: tab };
        firstSaved.set(mine.id, mine.savedAt);
        objects.put(mine);
        adopted.push(mine);
      }
    };
    await committed(objects.transaction);
    await noteCount();
    return adopted;
  });
}

/** A saved request's files, as Files again. */
export function savedFilesOf(record: SavedRequest): File[] {
  return record.files.map(
    (file) =>
      new File([file.bytes], file.name, { type: file.type, lastModified: file.lastModified }),
  );
}

/** Test hook: an empty store. */
export async function resetDesignOutboxStorage(): Promise<void> {
  savedFiles.clear();
  firstSaved.clear();
  if (!globalThis.indexedDB) return;
  const objects = await store("readwrite");
  objects.clear();
  await committed(objects.transaction);
  noteSavedDesignRequests(false);
}
