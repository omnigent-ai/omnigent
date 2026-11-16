import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserViewId } from "@/hooks/useBrowserTabs";
import { useChatStore } from "@/store/chatStore";
import type * as Identity from "@/lib/identity";

const identity = vi.hoisted(() => ({
  user: "alice" as string | null,
  // Who the server says is signed in; undefined follows `user`.
  signedIn: undefined as string | null | undefined,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof Identity>()),
  getCurrentUserId: () => identity.user,
  resolveIdentity: async () => identity.user,
  fetchSignedInUserId: async () =>
    identity.signedIn === undefined ? identity.user : identity.signedIn,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import {
  designOutboxSaved,
  forget,
  RESTORE_RETRY_MS,
  queueDesignRequest,
  resetDesignOutbox,
  restoreSavedDesignRequests,
  useDesignOutbox,
} from "./designOutbox";
import {
  resetDesignOutboxStorage,
  saveRequest,
  thisServer,
  thisTab,
  type SavedRequest,
} from "./designOutboxStorage";

const SESSION = "conv_saved";
const TAB = browserViewId(SESSION, "tab_1");
const initial = useChatStore.getState();

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("omnigent-design-outbox", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("requests", { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/** The records in the outbox's database, read straight from IndexedDB. */
async function records(): Promise<SavedRequest[]> {
  const db = await openDatabase();
  const all = await new Promise<SavedRequest[]>((resolve, reject) => {
    const request = db.transaction("requests").objectStore("requests").getAll();
    request.onsuccess = () => resolve(request.result as SavedRequest[]);
    request.onerror = () => reject(request.error);
  });
  db.close();
  return all;
}

/** Put a record as this tab (before a reload) or another tab left it. */
async function putRecord(record: SavedRequest): Promise<void> {
  const db = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction("requests", "readwrite");
    transaction.objectStore("requests").put(record);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
}

const record = (fields: Partial<SavedRequest> = {}): SavedRequest => ({
  id: "r1",
  tabId: TAB,
  conversationId: SESSION,
  label: "Annotate button",
  text: "Make it green",
  files: [],
  status: "waiting",
  savedAt: Date.now(),
  page: thisTab(),
  userId: "alice",
  server: thisServer(),
  ...fields,
});

const ids = () => useDesignOutbox.getState().requests.map((request) => request.id);
const savedIds = async () => (await records()).map((saved) => saved.id).sort();
const unloadAsks = () => {
  const event = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};

type LockTask = (lock: { name: string } | null) => unknown;

/** Web Locks as windows share them: exclusive per name; `ifAvailable` doesn't wait. */
function installLocks(openTabs: string[] = [], server = thisServer()) {
  const held = new Set(openTabs.map((tab) => `omnigent-design-outbox-page:${server}#${tab}`));
  const tails = new Map<string, Promise<unknown>>();
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: {
      request: (name: string, options: { ifAvailable?: boolean } | LockTask, task?: LockTask) => {
        const run = (typeof options === "function" ? options : task)!;
        if (typeof options === "object" && options.ifAvailable && held.has(name)) {
          return Promise.resolve(run(null));
        }
        const result = (tails.get(name) ?? Promise.resolve()).then(() => {
          held.add(name);
          return run({ name });
        });
        tails.set(
          name,
          result.then(
            () => held.delete(name),
            () => held.delete(name),
          ),
        );
        return result;
      },
      query: async () => ({ held: [...held].map((name) => ({ name })) }),
    },
  });
  return held;
}

beforeEach(async () => {
  identity.user = "alice";
  identity.signedIn = undefined;
  // No session is ready, so requests stay unsent.
  useChatStore.setState({ conversationId: null, boundAgentId: null });
  await designOutboxSaved();
  await resetDesignOutboxStorage();
});

afterEach(async () => {
  resetDesignOutbox();
  await designOutboxSaved();
  useChatStore.setState(initial, true);
  identity.listeners.clear();
  Reflect.deleteProperty(navigator, "locks");
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("design outbox storage", () => {
  it("saves an unsent request with its files, and forgets it once removed", async () => {
    const shot = new File(["png bytes"], "shot.png", { type: "image/png" });
    const id = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Note", files: [shot] });
    await designOutboxSaved();
    const [saved] = await records();
    expect(saved).toMatchObject({ id, status: "waiting", text: "Note", userId: "alice" });
    expect(new TextDecoder().decode(saved!.files[0]!.bytes)).toBe("png bytes");
    // Saved, so leaving doesn't ask.
    expect(unloadAsks()).toBe(false);
    forget(id);
    await designOutboxSaved();
    expect(await records()).toEqual([]);
  });

  it("counts a request as saved only once its save worked, and tries a failed one again", async () => {
    identity.user = null; // can't save without knowing who made it
    const first = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Big" });
    await designOutboxSaved();
    expect(await records()).toEqual([]);
    identity.user = "alice";
    const second = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Small" });
    await designOutboxSaved();
    await designOutboxSaved();
    expect(await savedIds()).toEqual([first, second].sort());
    expect(unloadAsks()).toBe(false);
  });

  it("doesn't count a save whose transaction aborted after the write (a full disk)", async () => {
    const put = IDBObjectStore.prototype.put;
    vi.spyOn(IDBObjectStore.prototype, "put").mockImplementationOnce(function (
      this: IDBObjectStore,
      ...args: Parameters<IDBObjectStore["put"]>
    ) {
      const request = put.apply(this, args);
      request.addEventListener("success", () => this.transaction.abort());
      return request;
    });
    queueDesignRequest({ tabId: TAB, label: "Draw", text: "Big" });
    await designOutboxSaved();
    expect(await records()).toEqual([]);
    expect(unloadAsks()).toBe(true);
  });

  it("keeps trying to delete a removed request's saved copy, so it can't come back", async () => {
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await designOutboxSaved();
    const failOnce = vi.spyOn(IDBObjectStore.prototype, "delete").mockImplementationOnce(() => {
      throw new Error("Storage is busy");
    });
    forget(id);
    await designOutboxSaved();
    await designOutboxSaved();
    expect(await savedIds()).toEqual([id]);
    // The delete is still owed, so leaving asks first.
    expect(unloadAsks()).toBe(true);
    failOnce.mockRestore();
    // The next change tries again.
    const other = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Blue" });
    await designOutboxSaved();
    await designOutboxSaved();
    expect(await savedIds()).toEqual([other]);
    expect(unloadAsks()).toBe(false);
  });

  it("asks before leaving while a sent request's saved copy isn't deleted yet", async () => {
    const send = vi.fn(async () => {});
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await designOutboxSaved();
    const failOnce = vi.spyOn(IDBObjectStore.prototype, "delete").mockImplementationOnce(() => {
      throw new Error("Storage is busy");
    });
    useChatStore.setState({
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [],
      startupPending: false,
      pendingUserMessages: [],
      pendingModelChange: null,
      send,
    });
    await vi.waitFor(() => expect(send).toHaveBeenCalled());
    await designOutboxSaved();
    await designOutboxSaved();
    expect(useDesignOutbox.getState().requests[0]).toMatchObject({ id, status: "sent" });
    expect(await savedIds()).toEqual([id]);
    expect(unloadAsks()).toBe(true);
    failOnce.mockRestore();
    // The next change deletes it.
    useChatStore.setState({ conversationId: null, boundAgentId: null });
    forget(queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Blue" }));
    await designOutboxSaved();
    await designOutboxSaved();
    expect(await savedIds()).toEqual([]);
    expect(unloadAsks()).toBe(false);
  });

  it("brings back this tab's requests after a reload, files included, none sending on its own", async () => {
    const bytes = new TextEncoder().encode("png bytes").buffer;
    await putRecord(
      record({
        id: "waiting",
        files: [{ name: "shot.png", type: "image/png", lastModified: 1, bytes }],
      }),
    );
    await putRecord(record({ id: "sending", status: "sending", savedAt: Date.now() + 1 }));
    await putRecord(
      record({
        id: "failed",
        status: "failed",
        error: "Couldn't send: x",
        refused: true,
        savedAt: Date.now() + 2,
      }),
    );
    // The session is ready: still nothing goes until the user says so.
    const send = vi.fn(async () => {});
    useChatStore.setState({
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [],
      send,
    });
    await restoreSavedDesignRequests();
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(send).not.toHaveBeenCalled();
    const [waiting, sending, failed] = ["waiting", "sending", "failed"].map((id) =>
      useDesignOutbox.getState().requests.find((request) => request.id === id),
    );
    expect(waiting).toMatchObject({ status: "failed", error: expect.stringContaining("Retry") });
    expect(await waiting!.files[0]!.text()).toBe("png bytes");
    expect(sending).toMatchObject({
      status: "failed",
      error: expect.stringContaining("may have reached the agent"),
    });
    // A refused one stays refused (it never reached the agent); the others may have.
    expect(failed).toMatchObject({
      status: "failed",
      error: "Couldn't send: x",
      delivery: "refused",
    });
    expect(sending).toMatchObject({ delivery: "unknown" });
  });

  it("brings back only the signed-in user's requests, once it's known who that is", async () => {
    await putRecord(record({ id: "alices" }));
    await putRecord(record({ id: "bobs", userId: "bob" }));
    identity.user = null;
    await restoreSavedDesignRequests();
    expect(ids()).toEqual([]);
    identity.user = "alice";
    for (const listener of identity.listeners) listener();
    await vi.waitFor(() => expect(ids()).toEqual(["alices"]));
    // Bob's stays saved for Bob.
    expect(await savedIds()).toEqual(["alices", "bobs"]);
  });

  it("brings back nothing when the server says another account is signed in now", async () => {
    await putRecord(record({ id: "alices" }));
    identity.signedIn = "bob"; // another window signed in as Bob; the cache still says Alice
    await restoreSavedDesignRequests();
    expect(ids()).toEqual([]);
    expect(await savedIds()).toEqual(["alices"]);
  });

  it("without Web Locks, takes back only this tab's own requests", async () => {
    await putRecord(record({ id: "mine" }));
    await putRecord(record({ id: "another_tabs", page: "another_tab" }));
    await restoreSavedDesignRequests();
    expect(ids()).toEqual(["mine"]);
    expect(await savedIds()).toEqual(["another_tabs", "mine"]);
  });

  it("with Web Locks, takes a closed tab's requests and leaves an open one's", async () => {
    installLocks(["open_tab"]);
    await putRecord(record({ id: "theirs", page: "open_tab" }));
    await putRecord(record({ id: "orphan", page: "closed_tab" }));
    await restoreSavedDesignRequests();
    expect(ids()).toEqual(["orphan"]);
    expect(await savedIds()).toEqual(["orphan", "theirs"]);
  });

  it("takes the requests of a tab now open on another server, its page lock naming its server", async () => {
    const held = installLocks(["moved_tab"], "https://other.example/proxy/1");
    await putRecord(record({ id: "left_here", page: "moved_tab" }));
    vi.resetModules();
    const page = await import("./designOutboxStorage");
    expect((await page.adoptSavedRequests("alice")).map((saved) => saved.id)).toEqual([
      "left_here",
    ]);
    expect(held).toContain(`omnigent-design-outbox-page:${thisServer()}#${page.thisTab()}`);
  });

  it("leaves requests saved for another server on this origin to that server", async () => {
    installLocks();
    await putRecord(record({ id: "other_servers", server: "https://other.example/proxy/1" }));
    await putRecord(record({ id: "this_servers" }));
    await restoreSavedDesignRequests();
    expect(ids()).toEqual(["this_servers"]);
    expect(await savedIds()).toEqual(["other_servers", "this_servers"]);
  });

  it("never takes or drops a request a live page holds: an open tab's, or this page's", async () => {
    installLocks(["open_tab"]);
    await putRecord(record({ id: "theirs", page: "open_tab" }));
    // This page's own, which it queued and still holds.
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Long-lived" });
    await designOutboxSaved();
    await restoreSavedDesignRequests();
    expect(await savedIds()).toEqual([id, "theirs"].sort());
    expect(ids()).toEqual([id]);
  });

  it("saves a request queued before the user was known, once they are", async () => {
    identity.user = null;
    const id = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Early" });
    await designOutboxSaved();
    expect(await savedIds()).toEqual([]);
    expect(unloadAsks()).toBe(true);
    identity.user = "alice";
    for (const listener of identity.listeners) listener();
    await vi.waitFor(async () => expect(await savedIds()).toEqual([id]));
    await designOutboxSaved();
    expect(unloadAsks()).toBe(false);
  });

  it("lets only one of two tabs starting together take a saved request, and splits a duplicated tab's id", async () => {
    installLocks();
    await putRecord(record({ id: "orphan", page: "closed_tab" }));
    // Two tabs: separate copies of the storage module sharing IndexedDB, the locks and
    // (as a duplicated tab does) sessionStorage, so they start with the same tab id.
    vi.resetModules();
    const tabA = await import("./designOutboxStorage");
    vi.resetModules();
    const tabB = await import("./designOutboxStorage");
    expect(tabA.thisTab()).toBe(tabB.thisTab());
    const [a, b] = await Promise.all([
      tabA.adoptSavedRequests("alice"),
      tabB.adoptSavedRequests("alice"),
    ]);
    expect([...a, ...b].map((saved) => saved.id)).toEqual(["orphan"]);
    expect(tabA.thisTab()).not.toBe(tabB.thisTab());
  });

  it("tries restoring again after storage fails", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await putRecord(record({ id: "saved" }));
    const failOnce = vi.spyOn(IDBObjectStore.prototype, "getAll").mockImplementationOnce(() => {
      throw new Error("Storage is busy");
    });
    await restoreSavedDesignRequests();
    expect(ids()).toEqual([]);
    failOnce.mockRestore();
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    await vi.waitFor(() => expect(ids()).toEqual(["saved"]));
  });

  it("saves a status change, so a reload sees why it failed", async () => {
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await designOutboxSaved();
    useDesignOutbox.setState((state) => ({
      requests: state.requests.map((request) =>
        request.id === id ? { ...request, status: "failed", error: "Couldn't send: x" } : request,
      ),
    }));
    await designOutboxSaved();
    expect((await records())[0]).toMatchObject({ status: "failed", error: "Couldn't send: x" });
    expect((await records())[0]!.refused).toBeUndefined();
    // And that it was refused, which is all that says it never reached the agent.
    useDesignOutbox.setState((state) => ({
      requests: state.requests.map((request) => ({ ...request, delivery: "refused" })),
    }));
    await designOutboxSaved();
    expect((await records())[0]).toMatchObject({ refused: true });
    // Saving one directly as sent removes it.
    await saveRequest({ ...useDesignOutbox.getState().requests[0]!, status: "sent" }, "alice");
    expect(await records()).toEqual([]);
  });
});
