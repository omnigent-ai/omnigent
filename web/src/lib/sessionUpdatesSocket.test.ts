import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HEARTBEAT_WATCHDOG_MS,
  HIDDEN_RECONNECT_MAX_MS,
  RECONNECT_MAX_MS,
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

/** Shadow `document.hidden` (jsdom defines it on the prototype). */
function setDocumentHidden(value: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

describe("sessionUpdatesSocket hidden-page backoff", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    // Pin jitter to its ceiling so each delay equals its base exactly.
    vi.spyOn(Math, "random").mockReturnValue(1);
  });

  afterEach(() => {
    sessionUpdatesSocket.stop();
    delete (document as { hidden?: boolean }).hidden;
    vi.clearAllTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  /**
   * Fail enough opens that the doubling backoff reaches whichever cap
   * applies. Starts from a fresh campaign: the afterEach `stop()` resets
   * `failedAttempts`, so ten doublings from the 250 ms base saturate either
   * cap deterministically regardless of test order.
   */
  function exhaustBackoff(): void {
    sessionUpdatesSocket.start();
    for (let i = 0; i < 10; i += 1) {
      latestWs().close();
      vi.advanceTimersByTime(HIDDEN_RECONNECT_MAX_MS);
    }
  }

  it("saturates at exactly HIDDEN_RECONNECT_MAX_MS while hidden", () => {
    setDocumentHidden(true);
    exhaustBackoff();

    // Schedule one more retry at the saturated cap (jitter pinned, so the
    // delay is exactly the cap).
    latestWs().close();
    const scheduled = FakeWebSocket.instances.length;

    // One tick short of the hidden cap: no attempt — a hidden page no longer
    // retries on the ≤5 s foreground cadence, nor on any shorter stretch.
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MAX_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(scheduled);

    // Crossing the cap fires exactly one reconnect.
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(scheduled + 1);
  });

  it("reconnects immediately when a hidden page becomes visible", () => {
    setDocumentHidden(true);
    exhaustBackoff();

    // A retry is pending at the saturated 60 s cap.
    latestWs().close();
    const scheduled = FakeWebSocket.instances.length;

    // Becoming visible skips the stretched delay and reconnects at once,
    // with no timer advance at all.
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(FakeWebSocket.instances.length).toBe(scheduled + 1);
  });

  it("does not open a second socket when visibility flips while one exists", () => {
    setDocumentHidden(true);
    sessionUpdatesSocket.start();
    const connecting = FakeWebSocket.instances.length;

    // Still CONNECTING: a visibility flip must not double-connect.
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(FakeWebSocket.instances.length).toBe(connecting);

    // Same while OPEN.
    latestWs().open();
    setDocumentHidden(true);
    document.dispatchEvent(new Event("visibilitychange"));
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(FakeWebSocket.instances.length).toBe(connecting);
  });

  it("starts a fresh backoff campaign after stop()/start()", () => {
    setDocumentHidden(true);
    exhaustBackoff();

    // A deliberate stop ends the campaign; the next start must not inherit
    // the saturated delay from the finished outage. Observed while visible:
    // a hidden page always waits the stretched cadence regardless of the
    // attempt count, so only the foreground ramp reveals the reset.
    setDocumentHidden(false);
    sessionUpdatesSocket.stop();
    sessionUpdatesSocket.start();

    latestWs().close();
    const scheduled = FakeWebSocket.instances.length;

    // First failure of the new campaign: base delay (250 ms with jitter
    // pinned), not the inherited cap.
    vi.advanceTimersByTime(250);
    expect(FakeWebSocket.instances.length).toBe(scheduled + 1);
  });

  it("waits the stretched hidden cadence from the very first failure", () => {
    // A page hidden EARLY in the backoff ramp must not keep dialing at the
    // fast foreground cadence until the doubling reaches the hidden cap —
    // that mid-ramp window still wakes the radio every few seconds.
    setDocumentHidden(true);
    sessionUpdatesSocket.start();
    latestWs().close();
    const scheduled = FakeWebSocket.instances.length;

    // One tick short of the hidden cap: no attempt, even on failure #1.
    vi.advanceTimersByTime(HIDDEN_RECONNECT_MAX_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(scheduled);

    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(scheduled + 1);
  });

  it("keeps the exact 5 s saturated cap when the page is visible", () => {
    setDocumentHidden(false);
    exhaustBackoff();

    latestWs().close();
    const scheduled = FakeWebSocket.instances.length;

    // Both sides of the boundary: still waiting one tick short of the cap
    // (so the foreground cadence wasn't accidentally shortened) ...
    vi.advanceTimersByTime(RECONNECT_MAX_MS - 1);
    expect(FakeWebSocket.instances.length).toBe(scheduled);

    // ... and reconnecting the tick it lands (not lengthened either).
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances.length).toBe(scheduled + 1);
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
