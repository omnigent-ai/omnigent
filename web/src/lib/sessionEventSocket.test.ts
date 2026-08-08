import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamSessionEventsWs, type WsStreamResult } from "./sessionEventSocket";

// Minimal stand-in for the browser WebSocket, mirroring sessionUpdatesSocket's
// test double: a real socket can't open in jsdom, and we need deterministic
// control over when frames / close arrive relative to the consumer's `await`.
class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  closeCount = 0;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = FakeWebSocket.CLOSED;
    // The real socket fires onclose asynchronously; emulate that so a caller
    // that closes then awaits still observes the terminal frame.
    this.onclose?.({ code: 1000 });
  }

  /** Test helper: deliver one server text frame. */
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }

  /** Test helper: server-initiated close with a given code. */
  serverClose(code: number): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code });
  }
}

function latestWs(): FakeWebSocket {
  const ws = FakeWebSocket.instances.at(-1);
  if (!ws) throw new Error("no WebSocket was constructed");
  return ws;
}

describe("streamSessionEventsWs", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("yields parsed events and marks a clean close on normal (1000) closure", async () => {
    const result: WsStreamResult = { sawCleanClose: false };
    const controller = new AbortController();
    const events: string[] = [];

    const iterable = streamSessionEventsWs("conv_a", controller.signal, undefined, result);
    // Start consuming before frames arrive; the FakeWebSocket delivers
    // synchronously into the generator's queue.
    const consumed = (async () => {
      for await (const ev of iterable) events.push(ev.type);
    })();

    // Yield a microtask so the generator installs its onmessage/onclose.
    await Promise.resolve();
    const ws = latestWs();
    ws.emit({ type: "response.output_text.delta", delta: "hi", item_id: "m1" });
    ws.serverClose(1000);

    await consumed;
    expect(events).toContain("text_delta");
    expect(result.sawCleanClose).toBe(true);
  });

  it("marks a drop (not clean) on an abnormal close code", async () => {
    const result: WsStreamResult = { sawCleanClose: false };
    const controller = new AbortController();

    const iterable = streamSessionEventsWs("conv_b", controller.signal, undefined, result);
    const consumed = (async () => {
      for await (const ev of iterable) void ev;
    })();

    await Promise.resolve();
    // 1001 (going away) is what the server sends on subscriber overflow.
    latestWs().serverClose(1001);

    await consumed;
    expect(result.sawCleanClose).toBe(false);
  });

  it("closes the socket and ends iteration when the signal aborts", async () => {
    const result: WsStreamResult = { sawCleanClose: false };
    const controller = new AbortController();

    const iterable = streamSessionEventsWs("conv_c", controller.signal, undefined, result);
    const consumed = (async () => {
      for await (const ev of iterable) void ev;
    })();

    await Promise.resolve();
    const ws = latestWs();
    controller.abort();

    await consumed;
    expect(ws.closeCount).toBeGreaterThanOrEqual(1);
  });

  it("does not open a socket if the signal is already aborted", async () => {
    const result: WsStreamResult = { sawCleanClose: false };
    const controller = new AbortController();
    controller.abort();

    for await (const ev of streamSessionEventsWs("conv_d", controller.signal, undefined, result)) {
      void ev;
    }
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
