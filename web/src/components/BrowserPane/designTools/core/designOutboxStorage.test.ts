import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { browserViewId } from "@/hooks/useBrowserTabs";
import { useChatStore, type SendOptions } from "@/store/chatStore";
import type * as Identity from "@/lib/identity";

const identity = vi.hoisted(() => ({
  user: "alice" as string | null,
  // Who the server says is signed in; undefined follows `user`.
  signedIn: undefined as string | null | undefined,
  // The server can't be reached to say; counts the asks.
  unreachable: false,
  asks: 0,
  // Set to hold the server's answers until it resolves.
  answerAfter: null as Promise<void> | null,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof Identity>()),
  getCurrentUserId: () => identity.user,
  resolveIdentity: async () => identity.user,
  fetchSignedInUserId: async () => {
    identity.asks += 1;
    await identity.answerAfter;
    if (identity.unreachable) return undefined;
    return identity.signedIn === undefined ? identity.user : identity.signedIn;
  },
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
  retryDesignRequest,
  useDesignOutbox,
} from "./designOutbox";
import { noteSavedDesignRequests } from "./designOutboxPresence";
import {
  deleteRequest,
  resetDesignOutboxStorage,
  SAVED_REQUEST_TTL_MS,
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
  identity.unreachable = false;
  identity.asks = 0;
  identity.answerAfter = null;
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

  it("reads a request's screenshot again after a failed read, so a later save keeps it", async () => {
    const shot = new File(["png bytes"], "shot.png", { type: "image/png" });
    const read = vi
      .spyOn(shot, "arrayBuffer")
      .mockRejectedValueOnce(new Error("Couldn't read the file"));
    const id = queueDesignRequest({ tabId: TAB, label: "Draw", text: "Note", files: [shot] });
    await designOutboxSaved();
    expect(await records()).toEqual([]);
    expect(unloadAsks()).toBe(true);
    // The next change saves it again, reading the screenshot afresh.
    const other = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Small" });
    await designOutboxSaved();
    await designOutboxSaved();
    expect(read).toHaveBeenCalledTimes(2);
    expect(await savedIds()).toEqual([id, other].sort());
    const saved = (await records()).find((entry) => entry.id === id);
    expect(new TextDecoder().decode(saved!.files[0]!.bytes)).toBe("png bytes");
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
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await designOutboxSaved();
    const busy = vi.spyOn(IDBObjectStore.prototype, "delete").mockImplementation(() => {
      throw new Error("Storage is busy");
    });
    forget(id);
    await designOutboxSaved();
    await designOutboxSaved();
    expect(await savedIds()).toEqual([id]);
    // The delete is still owed, so leaving asks first.
    expect(unloadAsks()).toBe(true);
    // It's tried again on its own, with nothing else in the outbox: still busy the first time.
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    await designOutboxSaved();
    expect(busy).toHaveBeenCalledTimes(2);
    busy.mockRestore();
    // The next try waits twice as long.
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    await designOutboxSaved();
    expect(await savedIds()).toEqual([id]);
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    await designOutboxSaved();
    expect(await savedIds()).toEqual([]);
    expect(useDesignOutbox.getState().requests).toEqual([]);
    expect(unloadAsks()).toBe(false);
  });

  it("leaves the saved hint on when another tab saves while a delete counts what's left", async () => {
    let otherTab: Promise<void> | undefined;
    const count = IDBObjectStore.prototype.count;
    vi.spyOn(IDBObjectStore.prototype, "count").mockImplementationOnce(function (
      this: IDBObjectStore,
      ...args
    ) {
      const counting = count.apply(this, args);
      // Another tab saves one as this count runs, and notes it once that commits.
      otherTab = putRecord(record({ id: "theirs", page: "other_tab" })).then(() =>
        noteSavedDesignRequests(true),
      );
      return counting;
    });
    await deleteRequest("gone");
    await otherTab;
    expect(await savedIds()).toEqual(["theirs"]);
    expect(localStorage.getItem("omnigent.designOutbox.saved")).toBe("1");
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
    // One saved waiting may have gone too: the reload can beat saving that it was sending.
    for (const request of [waiting, sending]) {
      expect(request).toMatchObject({
        status: "failed",
        error: expect.stringContaining("may have reached the agent"),
        delivery: "unknown",
      });
    }
    expect(await waiting!.files[0]!.text()).toBe("png bytes");
    // A refused one stays refused (it never reached the agent); the others may have.
    expect(failed).toMatchObject({
      status: "failed",
      error: "Couldn't send: x",
      delivery: "refused",
    });
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
    // Bob's isn't left on a machine Alice is signed in on now.
    expect(await savedIds()).toEqual(["alices"]);
  });

  it("asks the server who's signed in less often while it can't be reached", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await putRecord(record({ id: "saved" }));
    identity.unreachable = true;
    await restoreSavedDesignRequests();
    expect(identity.asks).toBe(1);
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    expect(identity.asks).toBe(2);
    // The next ask waits twice as long.
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    expect(identity.asks).toBe(2);
    identity.unreachable = false;
    await vi.advanceTimersByTimeAsync(RESTORE_RETRY_MS);
    expect(identity.asks).toBe(3);
    await vi.waitFor(() => expect(ids()).toEqual(["saved"]));
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

  it("drops anyone's request saved longer ago than it's kept, and keeps the rest", async () => {
    const old = Date.now() - SAVED_REQUEST_TTL_MS - 1;
    const elsewhere = "https://other.example/proxy/1";
    await putRecord(record({ id: "bobs_old", userId: "bob", server: elsewhere, savedAt: old }));
    await putRecord(record({ id: "alices_old", savedAt: old }));
    await putRecord(record({ id: "bobs_elsewhere", userId: "bob", server: elsewhere }));
    await putRecord(record({ id: "alices" }));
    await restoreSavedDesignRequests();
    expect(ids()).toEqual(["alices"]);
    // Another server's own users are its to sort out, once they're past the time.
    expect(await savedIds()).toEqual(["alices", "bobs_elsewhere"]);
  });

  it("restores without the adopt lock when the browser refuses it", async () => {
    installLocks();
    const { request } = navigator.locks;
    Object.defineProperty(navigator.locks, "request", {
      configurable: true,
      value: (...args: Parameters<typeof request>) =>
        typeof args[1] === "function"
          ? Promise.reject(new DOMException("Locks are unavailable", "SecurityError"))
          : request(...args),
    });
    await putRecord(record({ id: "saved" }));
    await restoreSavedDesignRequests();
    expect(ids()).toEqual(["saved"]);
  });

  it("still saves when the browser refuses this tab's lock", async () => {
    const held = installLocks();
    const { request } = navigator.locks;
    Object.defineProperty(navigator.locks, "request", {
      configurable: true,
      value: (...args: Parameters<typeof request>) =>
        typeof args[1] === "object" && args[1].ifAvailable
          ? Promise.reject(new DOMException("Locks are unavailable", "SecurityError"))
          : request(...args),
    });
    vi.resetModules();
    const fresh = await import("./designOutboxStorage");
    await fresh.saveRequest(
      {
        id: "refused_lock",
        tabId: TAB,
        conversationId: SESSION,
        label: "Draw",
        text: "Saved anyway",
        files: [],
        status: "waiting",
      },
      "alice",
    );
    expect(await savedIds()).toContain("refused_lock");
    expect(held.size).toBe(0);
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

  it("sends a waiting request only while the account that made it is still signed in", async () => {
    const send = vi.fn(async () => {});
    // Another window signed in as Bob; this one's cached user is still Alice.
    identity.signedIn = "bob";
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Alice's" });
    await designOutboxSaved();
    // The session gets ready for it: still not sent as Bob.
    useChatStore.setState({
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [],
      send,
    });
    const status = () => useDesignOutbox.getState().requests.find((r) => r.id === id);
    await vi.waitFor(() =>
      expect(status()).toMatchObject({
        status: "failed",
        delivery: "refused",
        error: expect.stringContaining("Another account is signed in"),
      }),
    );
    expect(send).not.toHaveBeenCalled();
    // Alice again: a Retry sends it.
    identity.signedIn = undefined;
    retryDesignRequest(id);
    await vi.waitFor(() => expect(status()?.status).not.toBe("failed"));
    expect(status()?.error).toBeUndefined();
  });

  it("doesn't post a request when another account signs in while its screenshot uploads", async () => {
    const posts: string[] = [];
    // Like the chat store: asks once the upload is done, and posts only if nothing holds it back.
    const send = vi.fn(
      async (text: string, _agent: string, _files?: File[], opts?: SendOptions) => {
        if (posts.length === 0 && identity.signedIn === undefined) identity.signedIn = "bob";
        const heldBack = await opts?.beforePost?.();
        if (heldBack) opts?.onError?.(heldBack, true);
        else posts.push(text);
      },
    );
    useChatStore.setState({
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle",
      sessionStatus: "idle",
      startupPending: false,
      queuedMessages: [],
      pendingUserMessages: [],
      pendingModelChange: null,
      send,
    });
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Alice's" });
    const status = () => useDesignOutbox.getState().requests.find((r) => r.id === id);
    await vi.waitFor(() =>
      expect(status()).toMatchObject({ status: "failed", delivery: "refused" }),
    );
    expect(status()?.error).toContain("Another account is signed in");
    expect(posts).toEqual([]);
    await designOutboxSaved();
    expect((await records())[0]).toMatchObject({ id, userId: "alice" });
    // Alice again: a Retry posts it.
    identity.signedIn = "alice";
    retryDesignRequest(id);
    await vi.waitFor(() => expect(posts).toEqual(["Alice's"]));
  });

  it("waits for a turn that starts while it asks who's signed in, just before it posts", async () => {
    const posts: string[] = [];
    let answer = () => {};
    // Like the chat store: asks once the upload is done, and posts only if nothing holds it back.
    const send = vi.fn(
      async (text: string, _agent: string, _files?: File[], opts?: SendOptions) => {
        if (posts.length === 0 && send.mock.calls.length === 1) {
          identity.answerAfter = new Promise((resolve) => {
            answer = resolve;
          });
        }
        const heldBack = await opts?.beforePost?.();
        if (heldBack) opts?.onError?.(heldBack, true);
        else posts.push(text);
      },
    );
    const ready = {
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle" as const,
      sessionStatus: "idle" as const,
      startupPending: false,
      queuedMessages: [],
      pendingUserMessages: [],
      pendingModelChange: null,
      send,
    };
    useChatStore.setState(ready);
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Alice's" });
    const status = () => useDesignOutbox.getState().requests.find((r) => r.id === id)?.status;
    await vi.waitFor(() => expect(send).toHaveBeenCalledOnce());
    // A turn starts while the server is asked; then it says Alice, still.
    useChatStore.setState({ sessionStatus: "running" });
    identity.answerAfter = null;
    answer();
    await vi.waitFor(() => expect(status()).toBe("waiting"));
    expect(posts).toEqual([]);
    useChatStore.setState({ sessionStatus: "idle" });
    await vi.waitFor(() => expect(posts).toEqual(["Alice's"]));
  });

  it("makes a request queued before anyone was known the signed-in account's before it goes", async () => {
    let delivered = () => {};
    const send = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          delivered = resolve;
        }),
    );
    identity.user = null;
    identity.signedIn = "bob";
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Bob's" });
    useChatStore.setState({
      conversationId: SESSION,
      boundAgentId: "agent_1",
      status: "idle",
      sessionStatus: "idle",
      startupPending: false,
      queuedMessages: [],
      pendingUserMessages: [],
      pendingModelChange: null,
      send,
    });
    await vi.waitFor(() => expect(send).toHaveBeenCalled());
    // This page learns of Alice while it sends: its saved copy stays Bob's.
    identity.user = "alice";
    for (const listener of identity.listeners) listener();
    await vi.waitFor(async () => expect(await records()).toMatchObject([{ id, userId: "bob" }]));
    delivered();
  });

  it("keeps a request's first save time when it's saved again, so it still expires on time", async () => {
    const madeAt = Date.now();
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Green" });
    await designOutboxSaved();
    vi.spyOn(Date, "now").mockReturnValue(madeAt + SAVED_REQUEST_TTL_MS / 2);
    useDesignOutbox.setState((state) => ({
      requests: state.requests.map((request) =>
        request.id === id ? { ...request, status: "failed", error: "Couldn't send: x" } : request,
      ),
    }));
    await designOutboxSaved();
    const [saved] = await records();
    expect(saved).toMatchObject({ id, status: "failed" });
    expect(saved!.savedAt).toBeLessThanOrEqual(madeAt + 1000);
  });

  it("keeps saving a request as its first owner's after the account changes", async () => {
    const id = queueDesignRequest({ tabId: TAB, label: "Annotate", text: "Alice's" });
    await designOutboxSaved();
    identity.user = "bob";
    useDesignOutbox.setState((state) => ({
      requests: state.requests.map((request) =>
        request.id === id ? { ...request, status: "failed", error: "Couldn't send: x" } : request,
      ),
    }));
    await designOutboxSaved();
    expect((await records())[0]).toMatchObject({ id, userId: "alice", status: "failed" });
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
