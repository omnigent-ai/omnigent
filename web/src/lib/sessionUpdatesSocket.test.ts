import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HIDDEN_RECONNECT_JITTER_MS,
  HIDDEN_RECONNECT_MS,
  RECONNECT_BASE_MS,
} from "./reconnectBackoff";
import {
  HEALTHY_CONNECTION_MS,
  HEARTBEAT_WATCHDOG_MS,
  nextPushedSession,
  sessionUpdatesSocket,
} from "./sessionUpdatesSocket";

// Minimal stand-in for the browser WebSocket: records sends/closes and lets
// the test drive the lifecycle (open, message) by hand. A real socket can't be
// opened in jsdom, and we need deterministic control over when frames arrive
// relative to the watchdog deadline — so this is the transport-level mock the
// testing guide allows.
class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readyState = 0; // CONNECTING
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closeCount = 0;
  readonly url: string;

  constructor(url: string) {
    // Plain field assignment, not a TS parameter property — the latter is
    // forbidden by `erasableSyntaxOnly` in tsconfig.app.json.
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(): void {
    // The watch-set send is irrelevant to the watchdog; ignore it.
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = 3; // CLOSED
    this.onclose?.();
  }

  /** Test helper: complete the handshake (fires the socket's onopen). */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /** Test helper: deliver one server text frame. */
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

/** The socket constructed most recently by start()/reconnect. */
function latestWs(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1);
  if (!ws) throw new Error("no WebSocket was constructed");
  return ws;
}

describe("sessionUpdatesSocket heartbeat watchdog", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    // Tear down the shared singleton's connection + timers so cases don't leak
    // into each other, then restore real timers/globals.
    sessionUpdatesSocket.stop();
    vi.clearAllTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.useRealTimers();
  });

  it("routes an OSS WebSocket by the same host key after reconnecting", async () => {
    vi.resetModules();
    vi.stubEnv("VITE_OMNIGENT_HOST_ROUTING", "true");
    const { resolveModalHost, setSessionHost } = await import("./sessionHost");
    const { sessionUpdatesSocket: socket } = await import("./sessionUpdatesSocket");
    setSessionHost("sess_oss", "host_oss");
    resolveModalHost();
    try {
      socket.start();
      const first = latestWs();
      expect(new URL(first.url).searchParams.get("omnigent_slice_key")).toBe("host_oss");
      first.open();
      first.close();
      vi.advanceTimersByTime(RECONNECT_CEILING_MS);
      expect(latestWs()).not.toBe(first);
      expect(latestWs().url).toBe(first.url);
    } finally {
      socket.stop();
      vi.resetModules();
    }
  });

  it("forces a reconnect after the watchdog window of total silence", () => {
    sessionUpdatesSocket.start();
    const ws = latestWs();
    ws.open();
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // One tick short of the deadline: still alive, not closed.
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    expect(ws.closeCount).toBe(0);
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // Crossing the deadline with zero frames trips the watchdog, which closes
    // the dead socket; onclose flips us to disconnected so consumers resume
    // their HTTP fallback poll.
    vi.advanceTimersByTime(1);
    expect(ws.closeCount).toBe(1);
    expect(sessionUpdatesSocket.isConnected()).toBe(false);

    // The close scheduled a reconnect; after the (jittered, ≤5 s) backoff a
    // fresh socket is constructed — the stream tries to come back, it doesn't
    // just give up.
    const before = FakeWebSocket.instances.length;
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(before + 1);
  });

  it("keeps the connection alive when a heartbeat arrives before the deadline", () => {
    sessionUpdatesSocket.start();
    const ws = latestWs();
    ws.open();

    // A heartbeat just before the deadline must reset the watchdog...
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    ws.emit({ type: "heartbeat" });

    // ...so advancing nearly another full window still doesn't close it. If the
    // watchdog hadn't reset, this second advance would have tripped it.
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1);
    expect(ws.closeCount).toBe(0);
    expect(sessionUpdatesSocket.isConnected()).toBe(true);

    // It still fires on a genuine stall after the last frame.
    vi.advanceTimersByTime(1);
    expect(ws.closeCount).toBe(1);
    expect(sessionUpdatesSocket.isConnected()).toBe(false);
  });
});

// Reconnect backoff is capped at 5 s + jitter; advancing past 5 s guarantees
// the scheduled reconnect timer has fired regardless of the random jitter.
const RECONNECT_CEILING_MS = 5_001;

function setPageHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

describe("sessionUpdatesSocket visibility-aware reconnect", () => {
  const HIDDEN_FLOOR_MS = HIDDEN_RECONNECT_MS - HIDDEN_RECONNECT_JITTER_MS;

  function fireVisibilityChange(hidden: boolean): void {
    setPageHidden(hidden);
    document.dispatchEvent(new Event("visibilitychange"));
  }

  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    setPageHidden(false);
  });

  afterEach(() => {
    sessionUpdatesSocket.stop();
    Reflect.deleteProperty(document, "visibilityState");
    Reflect.deleteProperty(document, "hidden");
    vi.clearAllTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("fires only the retry already pending when the page hides mid-outage", () => {
    sessionUpdatesSocket.start();
    const dialsBefore = FakeWebSocket.instances.length;

    // Arrange one foreground retry scheduled from the visible ramp, then hide.
    // The hidden cadence is attempt-independent, so a single pending retry is
    // enough to show which dial honours it.
    latestWs().close();
    fireVisibilityChange(true);

    // The retry scheduled while visible still fires once after hiding...
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);

    // ...but the retry scheduled while hidden waits the stretched cadence.
    latestWs().close();
    vi.advanceTimersByTime(HIDDEN_FLOOR_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MS - HIDDEN_FLOOR_MS + 1);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 2);
  });

  it("redials a healthy close at once while hidden and stretches only failed reopens", () => {
    sessionUpdatesSocket.start();
    latestWs().open();
    vi.advanceTimersByTime(HEALTHY_CONNECTION_MS);

    // A clean drop while hidden (e.g. the ingress recycle) keeps the visible
    // first-retry delay, so background notifications aren't held back.
    setPageHidden(true);
    const dialsBefore = FakeWebSocket.instances.length;
    latestWs().close();
    vi.advanceTimersByTime(RECONNECT_BASE_MS);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);

    // That reopen fails before opening, so the next retry waits the hidden cadence.
    latestWs().close();
    vi.advanceTimersByTime(HIDDEN_FLOOR_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MS - HIDDEN_FLOOR_MS + 1);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 2);
  });

  it("stretches retries while hidden when the server drops every socket right after it opens", () => {
    setPageHidden(true);
    sessionUpdatesSocket.start();

    // Accepted, then closed before it proved healthy (e.g. a crash loop): this
    // is a failed connection, so it waits the hidden cadence like a refused one.
    latestWs().open();
    vi.advanceTimersByTime(HEALTHY_CONNECTION_MS - 1);
    latestWs().close();
    vi.advanceTimersByTime(HIDDEN_FLOOR_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(1);
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MS - HIDDEN_FLOOR_MS + 1);
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("reconnects at once when the page becomes visible with a hidden retry pending", () => {
    setPageHidden(true);
    sessionUpdatesSocket.start();
    latestWs().close();
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(1);

    fireVisibilityChange(false);
    expect(FakeWebSocket.instances.length).toBe(2);
    // The stretched timer was cancelled, not left to dial a second time.
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MS);
    expect(FakeWebSocket.instances.length).toBe(2);
  });

  it("keeps a visible-ramp retry on its short delay across a hide/show flap", () => {
    sessionUpdatesSocket.start();
    const dialsBefore = FakeWebSocket.instances.length;

    // A retry scheduled while visible is pending; a quick hide then show must
    // not short-circuit it, or flapping during an outage would bypass the ramp.
    latestWs().close();
    fireVisibilityChange(true);
    fireVisibilityChange(false);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore);

    // It still fires once, on its own short ramp delay rather than instantly.
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);
  });

  it("does not redial on visibility while a socket is open or still connecting", () => {
    sessionUpdatesSocket.start();
    // Still connecting (no open yet): a visibility flip must not open a second socket.
    fireVisibilityChange(true);
    fireVisibilityChange(false);
    expect(FakeWebSocket.instances.length).toBe(1);

    // And once open, a visibility flip still leaves the single live socket alone.
    latestWs().open();
    fireVisibilityChange(true);
    fireVisibilityChange(false);
    expect(FakeWebSocket.instances.length).toBe(1);
  });

  it("drops a pending hidden retry on stop() and ramps from the base after start()", () => {
    setPageHidden(true);
    sessionUpdatesSocket.start();
    latestWs().close();
    // The hidden retry is scheduled 45-60 s out, so nothing redials within the cap.
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    expect(FakeWebSocket.instances.length).toBe(1);

    sessionUpdatesSocket.stop();
    // stop() cancels that pending hidden retry: nothing dials even past the cadence.
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MS * 2);
    expect(FakeWebSocket.instances.length).toBe(1);

    // A fresh start() ramps from the base; pin the jitter so an attempt-1 delay
    // fits the advance below while a carried-over attempt-2 delay would not.
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    setPageHidden(false);
    sessionUpdatesSocket.start();
    const dialsBefore = FakeWebSocket.instances.length;
    latestWs().close();
    vi.advanceTimersByTime(RECONNECT_BASE_MS + 1);
    expect(FakeWebSocket.instances.length).toBe(dialsBefore + 1);
  });
});

describe("nextPushedSession", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    sessionUpdatesSocket.stop();
    vi.unstubAllGlobals();
  });

  it("waits for the row the caller is expecting, not merely the next new one", async () => {
    sessionUpdatesSocket.start();
    const ws = latestWs();
    ws.open();
    const abort = new AbortController();
    const mine = nextPushedSession((item) => item.id === "conv_mine", abort.signal);

    // A session the caller didn't create lands first — another tab, a
    // scheduled task, one just shared with them. The stream announces those
    // identically, so settling on "something new arrived" would hand back the
    // wrong conversation (and the caller's first message with it).
    ws.emit({ type: "changed", items: [{ id: "conv_theirs" }] });
    ws.emit({ type: "changed", items: [{ id: "conv_mine", agent_id: "ag_1" }] });

    await expect(mine).resolves.toMatchObject({ id: "conv_mine", agent_id: "ag_1" });
  });

  it("resolves null once aborted so the caller falls back to its own result", async () => {
    sessionUpdatesSocket.start();
    latestWs().open();
    const abort = new AbortController();
    const mine = nextPushedSession(() => true, abort.signal);

    abort.abort();
    await expect(mine).resolves.toBeNull();

    // Settled for good: a late frame must not revive it, or a caller that
    // already committed to the authoritative id would be handed a second one.
    latestWs().emit({ type: "changed", items: [{ id: "conv_late" }] });
    await expect(mine).resolves.toBeNull();
  });
});
