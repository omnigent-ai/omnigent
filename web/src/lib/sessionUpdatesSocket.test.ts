import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HEARTBEAT_WATCHDOG_MS, sessionUpdatesSocket } from "./sessionUpdatesSocket";

// Mutable host-config + modal host so a case can flip the embed fetcher on and
// set the resolved modal host without touching real host state. Defaults (no
// fetcher, no host) keep the watchdog suite above on the standalone path, where
// buildUpdatesUrl emits no slice key — its behavior is unchanged.
let _mockFetcher: (() => void) | undefined;
let _mockModalHost: string | null = null;
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./host")>()),
  getOmnigentHostConfig: () => ({ fetcher: _mockFetcher }),
  // Standalone builds the ws URL from window.location, which jsdom sets to
  // http://localhost — mirror that so URL assertions are deterministic.
  resolveWebSocketUrl: (path: string) => `ws://localhost${path}`,
}));
vi.mock("@/lib/sessionHost", () => ({
  // buildUpdatesUrl reads the resolved modal host_id; the provider (not tested
  // here) owns resolving it, so the test drives it directly.
  modalHostId: () => _mockModalHost,
}));

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
    _mockFetcher = undefined;
    _mockModalHost = null;
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    // Tear down the shared singleton's connection + timers so cases don't leak
    // into each other, then restore real timers/globals.
    sessionUpdatesSocket.stop();
    vi.clearAllTimers();
    vi.unstubAllGlobals();
    vi.useRealTimers();
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

describe("sessionUpdatesSocket slice-key routing", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    _mockFetcher = undefined;
    _mockModalHost = null;
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    sessionUpdatesSocket.stop();
    vi.unstubAllGlobals();
  });

  it("keys the updates WS by the modal host on the managed (embedded) UI", () => {
    // Embedded (fetcher present) + a resolved modal host → the cross-host
    // updates WS rides ?omnigent_slice_key= so its standing rescan lands on that
    // host's replica (warm relay cache) rather than scattering by workspace-id.
    _mockFetcher = () => {};
    _mockModalHost = "host_abc";
    sessionUpdatesSocket.start();
    expect(latestWs().url).toBe(
      "ws://localhost/v1/sessions/updates?omnigent_slice_key=host_abc",
    );
  });

  it("omits the key on standalone (no embed fetcher)", () => {
    // No Dicer in front of a standalone/self-hosted server → no key.
    _mockFetcher = undefined;
    _mockModalHost = "host_abc";
    sessionUpdatesSocket.start();
    expect(latestWs().url).toBe("ws://localhost/v1/sessions/updates");
  });

  it("omits the key when the modal host is unresolved / null", () => {
    // Before resolution, or a zero-session user → no key → workspace-id default.
    _mockFetcher = () => {};
    _mockModalHost = null;
    sessionUpdatesSocket.start();
    expect(latestWs().url).toBe("ws://localhost/v1/sessions/updates");
  });

  it("reconnects with the SAME slice key (frozen modal host → no re-key churn)", () => {
    // The modal host is resolved once and never changes, so a reconnect must
    // rebuild the identical URL — the whole point of gating start() on the
    // resolution latch is that the persistent WS never re-keys.
    vi.useFakeTimers();
    _mockFetcher = () => {};
    _mockModalHost = "host_abc";
    sessionUpdatesSocket.start();
    const first = latestWs();
    const firstUrl = first.url;
    first.open();
    // Trip the watchdog to force a reconnect.
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS);
    vi.advanceTimersByTime(RECONNECT_CEILING_MS);
    const second = latestWs();
    expect(second).not.toBe(first);
    expect(second.url).toBe(firstUrl);
    expect(second.url).toBe("ws://localhost/v1/sessions/updates?omnigent_slice_key=host_abc");
    vi.useRealTimers();
  });
});
