import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionListWireItem } from "./sessionListCache";
import type { SessionUpdatesFrame } from "./sessionUpdatesSocket";
import {
  frameForTab,
  JOIN_TIMEOUT_MS,
  LEADER_SILENCE_MS,
  MEMBER_PRUNE_INTERVAL_MS,
  REJECTION_RETRY_MS,
  SERVER_MAX_WATCHED,
  SharedSessionUpdates,
  type SharedUpdatesEnvironment,
  type TabChannel,
  type UpdatesTransport,
} from "./sharedSessionUpdates";

// Several tabs in one test process. The browser pieces a real tab would share
// with the others are stood in for: Web Locks (exclusive, first come first
// served, released when the holder's promise settles), BroadcastChannel
// (delivered to every other tab, never to the sender), and each tab's
// WebSocket, which the test drives by hand to play the server.

type LockCallback = (lock: Lock | null) => unknown;

class FakeLocks {
  private readonly held = new Set<string>();
  private readonly queues = new Map<string, { run: LockCallback; aborted: boolean }[]>();

  request(name: string, options: { signal?: AbortSignal }, callback: LockCallback): Promise<void> {
    return new Promise((resolve, reject) => {
      const waiter = {
        run: (lock: Lock | null) => {
          Promise.resolve(callback(lock)).then(() => {
            this.held.delete(name);
            this.grantNext(name);
            resolve();
          });
        },
        aborted: false,
      };
      options.signal?.addEventListener("abort", () => {
        waiter.aborted = true;
        reject(new DOMException("aborted", "AbortError"));
      });
      const queue = this.queues.get(name) ?? [];
      this.queues.set(name, queue);
      queue.push(waiter);
      this.grantNext(name);
    });
  }

  /** When set, a query reads the locks at once but answers only after this settles. */
  queryDelay: Promise<void> | null = null;

  async query(): Promise<LockManagerSnapshot> {
    const held = [...this.held].map((name) => ({ name, mode: "exclusive" as const }));
    if (this.queryDelay !== null) await this.queryDelay;
    return { held };
  }

  private grantNext(name: string): void {
    if (this.held.has(name)) return;
    const queue = this.queues.get(name) ?? [];
    let next = queue.shift();
    while (next?.aborted) next = queue.shift();
    if (next === undefined) return;
    this.held.add(name);
    next.run({ name, mode: "exclusive" });
  }
}

class FakeChannelHub {
  private readonly channels = new Set<FakeChannel>();

  open(name: string): FakeChannel {
    const channel = new FakeChannel(this, name);
    this.channels.add(channel);
    return channel;
  }

  post(sender: FakeChannel, message: unknown): void {
    for (const channel of this.channels) {
      if (channel !== sender && channel.name === sender.name) {
        channel.onmessage?.({ data: structuredClone(message) });
      }
    }
  }

  close(channel: FakeChannel): void {
    this.channels.delete(channel);
  }
}

class FakeChannel implements TabChannel {
  onmessage: ((event: { data: unknown }) => void) | null = null;
  private readonly hub: FakeChannelHub;
  readonly name: string;

  constructor(hub: FakeChannelHub, name: string) {
    this.hub = hub;
    this.name = name;
  }

  postMessage(message: unknown): void {
    this.hub.post(this, message);
  }

  close(): void {
    this.hub.close(this);
  }
}

/**
 * One tab's WebSocket: records what the tab asked of it and plays the server.
 *
 * The server normally keeps up: it is watching whatever was last asked for.
 * With `serverLags` set it applies each watch request only when the test
 * pushes the snapshot that answers it, as a real server does.
 */
class FakeTransport implements UpdatesTransport {
  running = false;
  connected = false;
  watched: string[] = [];
  serverLags = false;
  /** Every watch-set sent to the server, in order. */
  readonly watches: string[][] = [];
  private serverWatched: ReadonlySet<string> = new Set();
  private readonly unanswered: string[][] = [];
  private readonly listeners = new Set<(frame: SessionUpdatesFrame) => void>();
  private readonly statusListeners = new Set<() => void>();

  start(): void {
    this.running = true;
  }

  stop(): void {
    this.running = false;
    this.setConnected(false);
  }

  isConnected(): boolean {
    return this.connected;
  }

  subscribeStatus(listener: () => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  setWatched(watched: string[], options?: { force?: boolean }): void {
    const unchanged = [...watched].sort().join(",") === [...this.watched].sort().join(",");
    if (unchanged && options?.force !== true) return;
    this.watched = watched;
    this.watches.push([...watched].sort());
    if (this.serverLags) this.unanswered.push(watched);
    else this.serverWatched = new Set(watched);
  }

  isWatching(id: string): boolean {
    return this.serverWatched.has(id);
  }

  subscribe(listener: (frame: SessionUpdatesFrame) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The socket opened or closed. */
  setConnected(connected: boolean): void {
    if (this.connected === connected) return;
    this.connected = connected;
    for (const listener of this.statusListeners) listener();
  }

  /** The server pushed a frame. A snapshot answers the oldest watch request still waiting. */
  push(frame: SessionUpdatesFrame): void {
    if (frame.type === "snapshot") {
      const answered = this.unanswered.shift();
      if (answered !== undefined) this.serverWatched = new Set(answered);
    }
    for (const listener of this.listeners) listener(frame);
  }
}

interface Tab {
  updates: SharedSessionUpdates;
  transport: FakeTransport;
  frames: SessionUpdatesFrame[];
}

function row(id: string): SessionListWireItem {
  return { id } as SessionListWireItem;
}

function ids(frame: SessionUpdatesFrame | undefined): string[] {
  if (frame?.type === "snapshot" || frame?.type === "changed") {
    return frame.items.map((item) => item.id);
  }
  return frame?.type === "removed" ? frame.ids : [];
}

describe("SharedSessionUpdates", () => {
  let locks: FakeLocks;
  let hub: FakeChannelHub;
  let tabCount: number;

  function openTab(
    watched: string[],
    environment: Partial<SharedUpdatesEnvironment> | null = {},
    eligibility?: { known: () => boolean; pending: Promise<void> },
  ): Tab {
    tabCount += 1;
    const tabId = `tab-${tabCount}`;
    const transport = new FakeTransport();
    const updates = new SharedSessionUpdates(
      transport,
      () =>
        environment === null || eligibility?.known() === false
          ? null
          : {
              locks: locks as unknown as SharedUpdatesEnvironment["locks"],
              openChannel: (name) => hub.open(name),
              scope: "server-a|user-a",
              tabId,
              ...environment,
            },
      () => (eligibility?.known() === false ? eligibility.pending : null),
    );
    const frames: SessionUpdatesFrame[] = [];
    updates.subscribe((frame) => frames.push(frame));
    updates.setWatched(watched);
    updates.start();
    return { updates, transport, frames };
  }

  /** Let lock grants and the messages they cause settle. */
  async function settle(): Promise<void> {
    await vi.advanceTimersByTimeAsync(0);
  }

  beforeEach(() => {
    vi.useFakeTimers();
    locks = new FakeLocks();
    hub = new FakeChannelHub();
    tabCount = 0;
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("opens one socket for two tabs, watching both tabs' sessions", async () => {
    const first = openTab(["a", "b"]);
    await settle();
    const second = openTab(["b", "c"]);
    await settle();

    expect(first.transport.running).toBe(true);
    expect(second.transport.running).toBe(false);
    expect(first.transport.watched.toSorted()).toEqual(["a", "b", "c"]);
  });

  it("gives each tab only the rows it is showing", async () => {
    const first = openTab(["a", "b"]);
    await settle();
    const second = openTab(["b", "c"]);
    await settle();

    first.transport.push({ type: "changed", items: [row("a"), row("b"), row("c")] });

    expect(ids(first.frames.at(-1))).toEqual(["a", "b"]);
    expect(ids(second.frames.at(-1))).toEqual(["b", "c"]);
  });

  it("keeps a late update for a session a tab just stopped showing away from the other tabs", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();
    first.transport.serverLags = true;
    const firstSaw = first.frames.length;
    const secondSaw = second.frames.length;

    // The second tab adds c, then drops b. The server has heard neither yet.
    second.updates.setWatched(["b", "c"]);
    second.updates.setWatched(["c"]);
    first.transport.push({ type: "changed", items: [row("b")] });
    // Its answer to the first request still lists b, and it still watches b after it.
    first.transport.push({ type: "snapshot", items: [row("a"), row("b"), row("c")] });
    first.transport.push({ type: "changed", items: [row("b")] });

    // b is an ordinary watched row on its way out, not a session the server announced.
    expect(first.frames.slice(firstSaw).flatMap(ids)).not.toContain("b");
    expect(second.frames.slice(secondSaw).flatMap(ids)).not.toContain("b");

    // Once the server has answered the request that dropped b, an update for b is an announcement.
    first.transport.push({ type: "snapshot", items: [row("a"), row("c")] });
    first.transport.push({ type: "changed", items: [row("b")] });
    expect(ids(first.frames.at(-1))).toEqual(["b"]);
    expect(ids(second.frames.at(-1))).toEqual(["b"]);
  });

  it("delivers an announcement to every tab though one tab has just started showing that session", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();
    first.transport.serverLags = true;

    // The server announces x, unwatched. Before the frame arrives, the second tab starts showing x.
    second.updates.setWatched(["b", "x"]);
    first.transport.push({ type: "changed", items: [row("x")] });

    // The server wasn't watching x when it sent that, so it is still news to the first tab.
    expect(ids(first.frames.at(-1))).toEqual(["x"]);
    expect(ids(second.frames.at(-1))).toEqual(["x"]);
  });

  it("asks the server to restate a session a second tab starts showing, though the socket already watches it", async () => {
    const first = openTab(["a", "b"]);
    await settle();
    const second = openTab(["a"]);
    await settle();
    const sentBefore = first.transport.watches.length;

    second.updates.setWatched(["a", "b"]);

    // Same union, sent again, and the snapshot it produces reaches the second tab.
    expect(first.transport.watches.length).toBe(sentBefore + 1);
    expect(first.transport.watches.at(-1)).toEqual(["a", "b"]);
    first.transport.push({ type: "snapshot", items: [row("a"), row("b")] });
    expect(ids(second.frames.at(-1))).toEqual(["a", "b"]);
  });

  it("re-sends the watch-set when another tab starts showing a session, so the server restates it", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["a"]);
    await settle();
    const sentBefore = first.transport.watches.length;

    // The union is unchanged, but the second tab needs these rows' current state.
    second.updates.setWatched(["a"]);
    expect(first.transport.watches.length).toBe(sentBefore);
    second.updates.setWatched(["a", "b"]);
    expect(first.transport.watches.at(-1)).toEqual(["a", "b"]);
    const afterAdd = first.transport.watches.length;
    second.updates.setWatched(["a"]);
    // Dropping an id narrows the set without asking for a snapshot.
    expect(first.transport.watches.at(-1)).toEqual(["a"]);
    expect(first.transport.watches.length).toBe(afterAdd + 1);
  });

  it("delivers a session the server announces to every tab", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();

    // Not in any watch-set: a session just created or shared with the user.
    first.transport.push({ type: "changed", items: [row("new")] });

    expect(ids(first.frames.at(-1))).toEqual(["new"]);
    expect(ids(second.frames.at(-1))).toEqual(["new"]);
  });

  it("mirrors the shared socket's connection state in a tab without one", async () => {
    const first = openTab(["a"]);
    await settle();
    first.transport.setConnected(true);
    const second = openTab(["b"]);
    await settle();
    const changes: boolean[] = [];
    second.updates.subscribeStatus(() => changes.push(second.updates.isConnected()));

    expect(second.updates.isConnected()).toBe(true);
    first.transport.setConnected(false);
    first.transport.setConnected(true);

    expect(changes).toEqual([false, true]);
  });

  it("hands the socket to another tab when the tab holding it closes", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    const third = openTab(["c"]);
    await settle();

    first.updates.stop();
    await settle();

    expect(first.transport.running).toBe(false);
    expect(second.transport.running).toBe(true);
    expect(third.transport.running).toBe(false);
    // The closed tab's sessions are gone; the remaining tabs' are watched.
    expect(second.transport.watched.toSorted()).toEqual(["b", "c"]);
    second.transport.push({ type: "changed", items: [row("c")] });
    expect(ids(third.frames.at(-1))).toEqual(["c"]);
  });

  it("still announces a session shared again after a tab on its own socket took over", async () => {
    const many = Array.from({ length: SERVER_MAX_WATCHED }, (_, index) => `s${index}`);
    const first = openTab(many);
    await settle();
    // Too many sessions to share, so this tab runs its own, connected socket.
    const second = openTab(["x", "y"]);
    await settle();
    second.transport.setConnected(true);
    second.transport.push({ type: "snapshot", items: [row("x"), row("y")] });

    // The tab holding the shared socket closes; this tab's socket takes over as it is.
    first.updates.stop();
    await settle();
    expect(second.transport.running).toBe(true);

    // Access to x is revoked: the tab stops watching it, and the server restates the rest.
    second.transport.push({ type: "removed", ids: ["x"] });
    second.updates.setWatched(["y"]);
    second.transport.push({ type: "snapshot", items: [row("y")] });

    // x is shared again, which the server announces on its own.
    second.transport.push({ type: "changed", items: [row("x")] });
    expect(ids(second.frames.at(-1))).toEqual(["x"]);
  });

  it("keeps a tab that joined while the leader was checking which tabs are still open", async () => {
    const first = openTab(["a"]);
    await settle();
    openTab(["b"]);
    await settle();
    let answer: () => void = () => {};
    locks.queryDelay = new Promise<void>((resolve) => {
      answer = resolve;
    });
    // The check reads the open tabs now, and gets its answer later.
    await vi.advanceTimersByTimeAsync(MEMBER_PRUNE_INTERVAL_MS);

    const third = openTab(["c"]);
    await settle();
    expect(first.transport.watched).toContain("c");
    answer();
    await settle();

    // The answer predates the new tab, which is open and still relies on the shared socket.
    expect(first.transport.watched).toContain("c");
    expect(third.transport.running).toBe(false);
    first.transport.push({ type: "changed", items: [row("c")] });
    expect(ids(third.frames.at(-1))).toEqual(["c"]);
  });

  it("stops watching a tab's sessions when it leaves", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();

    second.updates.stop();

    expect(first.transport.watched).toEqual(["a"]);
  });

  it("stops watching the sessions of a tab that closed without saying so", async () => {
    const first = openTab(["a"]);
    await settle();
    openTab(["b"], { tabId: "crashed" });
    await settle();
    expect(first.transport.watched.toSorted()).toEqual(["a", "b"]);

    // A crashed tab posts nothing; the browser just releases its locks.
    const crashed = [...locks["held"]].find((name) => name.endsWith(":crashed"));
    locks["held"].delete(crashed ?? "");
    await vi.advanceTimersByTimeAsync(MEMBER_PRUNE_INTERVAL_MS);

    expect(first.transport.watched).toEqual(["a"]);
  });

  it("falls back to the tab's own socket when the tab holding the shared one goes silent", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();
    expect(second.transport.running).toBe(false);

    // A frozen tab keeps the lock but relays nothing, not even heartbeats.
    await vi.advanceTimersByTimeAsync(LEADER_SILENCE_MS);

    expect(second.transport.running).toBe(true);
    expect(second.transport.watched).toEqual(["b"]);
    second.transport.push({ type: "changed", items: [row("b")] });
    expect(ids(second.frames.at(-1))).toEqual(["b"]);
    expect(first.transport.running).toBe(true);
  });

  it("returns to the shared socket once the silent tab relays again", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();
    await vi.advanceTimersByTimeAsync(LEADER_SILENCE_MS);
    expect(second.transport.running).toBe(true);
    const sentBefore = first.transport.watches.length;

    // The silent tab wakes with an update for the other tab's session.
    first.transport.push({ type: "changed", items: [row("b")] });

    // The union is unchanged, but the returning tab's rows are asked for again.
    expect(first.transport.watches.length).toBe(sentBefore + 1);
    expect(first.transport.watched.toSorted()).toEqual(["a", "b"]);
    // Until that snapshot is relayed, the tab keeps its own socket and its updates.
    expect(second.transport.running).toBe(true);
    second.transport.push({ type: "changed", items: [row("b")] });
    expect(ids(second.frames.at(-1))).toEqual(["b"]);

    first.transport.push({ type: "snapshot", items: [row("a"), row("b")] });

    // The resumed row arrived, and only now is the tab's own socket closed.
    expect(second.frames.at(-1)).toEqual({ type: "snapshot", items: [row("b")] });
    expect(second.transport.running).toBe(false);
  });

  it("keeps relayed heartbeats from tripping the silence fallback", async () => {
    const first = openTab(["a"]);
    await settle();
    const second = openTab(["b"]);
    await settle();

    // The server's heartbeat, every 30 s, for twice the silence window.
    const heartbeat = setInterval(() => first.transport.push({ type: "heartbeat" }), 30_000);
    await vi.advanceTimersByTimeAsync(LEADER_SILENCE_MS * 2);
    clearInterval(heartbeat);

    expect(second.transport.running).toBe(false);
  });

  it("opens the tab's own socket when no tab answers in time", async () => {
    // Another tab holds the lock but never answers, e.g. it is frozen.
    void locks.request("omnigent:session-updates:v1:leader:server-a|user-a", {}, () => {
      return new Promise(() => {});
    });
    const tab = openTab(["a"]);
    await settle();
    expect(tab.transport.running).toBe(false);

    await vi.advanceTimersByTimeAsync(JOIN_TIMEOUT_MS);

    expect(tab.transport.running).toBe(true);
    expect(tab.transport.watched).toEqual(["a"]);
  });

  it("keeps a tab on its own socket when the tabs' sessions exceed the server's watch limit", async () => {
    const many = Array.from({ length: SERVER_MAX_WATCHED }, (_, index) => `s${index}`);
    const first = openTab(many);
    await settle();
    const second = openTab(["extra"]);
    await settle();

    expect(second.transport.running).toBe(true);
    expect(second.transport.watched).toEqual(["extra"]);
    expect(first.transport.watched).toHaveLength(SERVER_MAX_WATCHED);
    expect(first.transport.watched).not.toContain("extra");

    // Once its sessions fit, the tab joins the shared socket.
    second.updates.setWatched(["s0"]);
    first.transport.push({ type: "snapshot", items: many.map(row) });
    expect(second.transport.running).toBe(false);
  });

  it("keeps its own socket through a snapshot taken before the shared socket watched its sessions", async () => {
    const many = Array.from({ length: SERVER_MAX_WATCHED }, (_, index) => `s${index}`);
    const first = openTab(many);
    await settle();
    const second = openTab(["extra"]);
    await settle();
    first.transport.serverLags = true;

    // Room opens up, and the turned-away tab asks again with a session that fits.
    first.updates.setWatched(many.slice(1));
    second.updates.setWatched(["later"]);
    expect(first.transport.watched).toContain("later");

    // The server's answer to the earlier request doesn't have that session yet.
    first.transport.push({ type: "snapshot", items: many.slice(1).map(row) });
    expect(second.transport.running).toBe(true);

    first.transport.push({ type: "snapshot", items: [...many.slice(1), "later"].map(row) });
    expect(second.transport.running).toBe(false);
    expect(ids(second.frames.at(-1))).toEqual(["later"]);
  });

  it("keeps a late update away from other tabs when a tab's own socket takes over mid-request", async () => {
    const many = Array.from({ length: SERVER_MAX_WATCHED }, (_, index) => `s${index}`);
    const first = openTab(many);
    await settle();
    // Too many sessions to share, so this tab runs its own socket.
    const second = openTab(["x", "y"]);
    await settle();
    second.transport.serverLags = true;
    // It stops showing x; the server hasn't answered when the shared socket's tab closes.
    second.updates.setWatched(["y"]);
    first.updates.stop();
    await settle();
    const third = openTab(["z"]);
    await settle();
    const thirdSaw = third.frames.length;

    // Still watching x, the server sends an update for it.
    second.transport.push({ type: "changed", items: [row("x")] });
    expect(third.frames.slice(thirdSaw).flatMap(ids)).not.toContain("x");

    // After the answers to both requests, an update for x is an announcement.
    second.transport.push({ type: "snapshot", items: [row("y")] });
    second.transport.push({ type: "snapshot", items: [row("y"), row("z")] });
    second.transport.push({ type: "changed", items: [row("x")] });
    expect(ids(third.frames.at(-1))).toEqual(["x"]);
  });

  it("asks again after a while, since another tab may have freed room", async () => {
    const most = Array.from({ length: SERVER_MAX_WATCHED - 1 }, (_, index) => `s${index}`);
    const first = openTab(most);
    await settle();
    const second = openTab(["b"]);
    await settle();
    const third = openTab(["c"]);
    await settle();
    expect(third.transport.running).toBe(true);

    // The second tab leaves, and the turned-away tab's own sessions haven't changed.
    second.updates.stop();
    first.transport.push({ type: "heartbeat" });
    expect(first.transport.watched).not.toContain("c");

    await vi.advanceTimersByTimeAsync(REJECTION_RETRY_MS);
    first.transport.push({ type: "heartbeat" });
    expect(first.transport.watched).toContain("c");
    first.transport.push({ type: "snapshot", items: [row("c")] });
    expect(third.transport.running).toBe(false);
  });

  it("never drops the socket-holding tab's own sessions to fit another tab's", async () => {
    const many = Array.from({ length: SERVER_MAX_WATCHED - 1 }, (_, index) => `s${index}`);
    const first = openTab(many);
    await settle();
    openTab(["x", "y"]);
    await settle();

    expect(first.transport.watched.toSorted()).toEqual(many.toSorted());
  });

  it.each([
    ["another user", "server-a|user-b"],
    ["another server", "server-b|user-a"],
  ])("does not share a socket with a tab of %s", async (_label, scope) => {
    const first = openTab(["a"]);
    const other = openTab(["b"], { scope });
    await settle();

    expect(first.transport.running).toBe(true);
    expect(other.transport.running).toBe(true);
    expect(first.transport.watched).toEqual(["a"]);
    first.transport.push({ type: "changed", items: [row("b")] });
    expect(other.frames).toEqual([]);
  });

  it("starts sharing, without a restart, once the server's features have loaded", async () => {
    let known = false;
    let loaded: () => void = () => {};
    const eligibility = {
      known: () => known,
      pending: new Promise<void>((resolve) => {
        loaded = resolve;
      }),
    };
    // Both tabs start before the flag can be read, so each opens its own socket.
    const first = openTab(["a"], {}, eligibility);
    const second = openTab(["b"], {}, eligibility);
    await settle();
    first.transport.setConnected(true);
    second.transport.setConnected(true);
    expect(second.transport.running).toBe(true);

    known = true;
    loaded();
    await settle();

    // One tab's socket becomes the shared one; the other keeps its own until its rows are restated.
    expect(first.transport.watched.toSorted()).toEqual(["a", "b"]);
    expect(second.transport.running).toBe(true);
    first.transport.push({ type: "snapshot", items: [row("a"), row("b")] });
    expect(second.transport.running).toBe(false);
    expect(ids(second.frames.at(-1))).toEqual(["b"]);
  });

  it("stays on its own socket when the loaded features don't include sharing", async () => {
    const eligibility = { known: () => false, pending: Promise.resolve() };
    const first = openTab(["a"], {}, eligibility);
    const second = openTab(["b"], {}, eligibility);
    await settle();

    expect(first.transport.running).toBe(true);
    expect(second.transport.running).toBe(true);
    expect(first.transport.watched).toEqual(["a"]);
  });

  it("uses the tab's own socket, unshared, when sharing is unavailable", async () => {
    const shared = openTab(["a"]);
    await settle();
    const alone = openTab(["b"], null);
    await settle();

    expect(alone.transport.running).toBe(true);
    expect(alone.transport.watched).toEqual(["b"]);
    expect(shared.transport.watched).toEqual(["a"]);
    alone.transport.push({ type: "snapshot", items: [row("b")] });
    expect(ids(alone.frames.at(-1))).toEqual(["b"]);

    alone.updates.stop();
    expect(alone.transport.running).toBe(false);
  });

  it("reports disconnected and delivers nothing after stop()", async () => {
    const first = openTab(["a"]);
    await settle();
    first.transport.setConnected(true);
    const second = openTab(["a"]);
    await settle();
    expect(second.updates.isConnected()).toBe(true);

    second.updates.stop();
    first.transport.push({ type: "changed", items: [row("a")] });

    expect(second.updates.isConnected()).toBe(false);
    expect(second.frames).toEqual([]);
  });
});

describe("frameForTab", () => {
  const none: ReadonlySet<string> = new Set();

  it("drops a snapshot taken before the shared socket watched this tab's sessions", () => {
    // Delivered, it would read as "every session this tab shows was removed".
    const snapshot: SessionUpdatesFrame = { type: "snapshot", items: [row("other")] };
    expect(frameForTab(snapshot, new Set(["mine"]), none)).toBeNull();
  });

  it("keeps an empty snapshot for a tab that shows no sessions", () => {
    const snapshot: SessionUpdatesFrame = { type: "snapshot", items: [] };
    expect(frameForTab(snapshot, none, none)).toBe(snapshot);
  });

  it("reports only this tab's sessions as removed", () => {
    const removed: SessionUpdatesFrame = { type: "removed", ids: ["mine", "other"] };
    expect(frameForTab(removed, new Set(["mine"]), none)).toEqual({
      type: "removed",
      ids: ["mine"],
    });
    expect(frameForTab({ type: "removed", ids: ["other"] }, new Set(["mine"]), none)).toBeNull();
  });

  it("passes list-wide notices through to every tab", () => {
    for (const frame of [
      { type: "hosts_changed" },
      { type: "projects_changed" },
      { type: "heartbeat" },
      { type: "access_changed", ids: ["other"] },
    ] satisfies SessionUpdatesFrame[]) {
      expect(frameForTab(frame, new Set(["mine"]), none)).toBe(frame);
    }
  });
});
