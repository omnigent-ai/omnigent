// Tests for the dictation socket protocol parsing. The DictationSession
// transport itself (mic + AudioWorklet + WebSocket) can't run in jsdom;
// its behavior against the component is pinned in ComposerMicButton.test.tsx
// with a mocked session, and the full loop runs in the Playwright e2e test
// against the server's fake engine.

import { afterEach, describe, expect, it, vi } from "vitest";
import { DictationSession, getDictationMediaStream, parseDictationEvent } from "./dictation";

afterEach(() => {
  vi.restoreAllMocks();
  delete (navigator as { mediaDevices?: unknown }).mediaDevices;
});

const installMediaDevices = (getUserMedia: ReturnType<typeof vi.fn>) => {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
};

describe("parseDictationEvent", () => {
  it("parses the transcript event shapes", () => {
    expect(parseDictationEvent('{"type":"ready"}')).toEqual({ type: "ready" });
    expect(parseDictationEvent('{"type":"partial","text":"hel"}')).toEqual({
      type: "partial",
      text: "hel",
    });
    expect(parseDictationEvent('{"type":"final","text":"hello."}')).toEqual({
      type: "final",
      text: "hello.",
    });
    expect(parseDictationEvent('{"type":"stopped","text":""}')).toEqual({
      type: "stopped",
      text: "",
    });
    expect(parseDictationEvent('{"type":"error","message":"boom"}')).toEqual({
      type: "error",
      message: "boom",
    });
  });

  it("returns null for malformed or unknown frames", () => {
    expect(parseDictationEvent("not json")).toBeNull();
    expect(parseDictationEvent("42")).toBeNull();
    expect(parseDictationEvent("null")).toBeNull();
    expect(parseDictationEvent('{"type":"future-thing"}')).toBeNull();
    // Known types with a missing/mistyped payload are dropped, not crashed on.
    expect(parseDictationEvent('{"type":"partial"}')).toBeNull();
    expect(parseDictationEvent('{"type":"partial","text":7}')).toBeNull();
    expect(parseDictationEvent('{"type":"error"}')).toBeNull();
  });
});

describe("getDictationMediaStream", () => {
  it("uses the default microphone constraints when no device is selected", async () => {
    const stream = {} as MediaStream;
    const getUserMedia = vi.fn().mockResolvedValue(stream);
    installMediaDevices(getUserMedia);

    await expect(getDictationMediaStream(null)).resolves.toBe(stream);
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  });

  it.each(["NotFoundError", "OverconstrainedError"])(
    "does not silently replace a missing selected microphone after %s",
    async (name) => {
      const error = new DOMException("missing", name);
      const getUserMedia = vi.fn().mockRejectedValue(error);
      installMediaDevices(getUserMedia);

      await expect(getDictationMediaStream("mic-2")).rejects.toBe(error);
      expect(getUserMedia).toHaveBeenCalledOnce();
      expect(getUserMedia).toHaveBeenCalledWith({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          deviceId: { exact: "mic-2" },
        },
      });
    },
  );

  it.each(["NotAllowedError", "SecurityError"])(
    "does not retry permission error %s",
    async (name) => {
      const error = new DOMException("denied", name);
      const getUserMedia = vi.fn().mockRejectedValue(error);
      installMediaDevices(getUserMedia);

      await expect(getDictationMediaStream("mic-2")).rejects.toBe(error);
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    },
  );
});

describe("DictationSession.start", () => {
  const events = { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() };

  it("aborts while waiting for ready and closes the socket and capture", async () => {
    const stop = vi.fn();
    installMediaDevices(
      vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] } as unknown as MediaStream),
    );
    const close = vi.fn();
    const constructed = vi.fn();
    class FakeWebSocket {
      static OPEN = 1;
      readyState = 0;
      binaryType = "";
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      close = close;
      constructor() {
        constructed();
      }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const controller = new AbortController();

    const starting = DictationSession.start(events, { signal: controller.signal });
    await vi.waitFor(() => expect(constructed).toHaveBeenCalledOnce());
    controller.abort();

    await expect(starting).rejects.toMatchObject({ name: "AbortError" });
    expect(close).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });

  it("stops capture that resolves after an abort during media acquisition", async () => {
    const stop = vi.fn();
    let resolveCapture: ((stream: MediaStream) => void) | undefined;
    installMediaDevices(
      vi.fn(
        () =>
          new Promise<MediaStream>((resolve) => {
            resolveCapture = resolve;
          }),
      ),
    );
    const controller = new AbortController();

    const starting = DictationSession.start(events, { signal: controller.signal });
    controller.abort();
    await expect(starting).rejects.toMatchObject({ name: "AbortError" });
    resolveCapture?.({ getTracks: () => [{ stop }] } as unknown as MediaStream);
    await vi.waitFor(() => expect(stop).toHaveBeenCalledOnce());

    expect(stop).toHaveBeenCalledOnce();
  });

  it("aborts during worklet setup and closes all acquired resources", async () => {
    const stop = vi.fn();
    installMediaDevices(
      vi.fn().mockResolvedValue({ getTracks: () => [{ stop }] } as unknown as MediaStream),
    );
    const wsClose = vi.fn();
    class FakeWebSocket {
      static OPEN = 1;
      readyState = FakeWebSocket.OPEN;
      binaryType = "";
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      close = wsClose;
      addEventListener() {}
      removeEventListener() {}
      constructor() {
        queueMicrotask(() => this.onmessage?.({ data: '{"type":"ready"}' } as MessageEvent));
      }
    }
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const contextClose = vi.fn(function (this: { state: AudioContextState }) {
      this.state = "closed";
      return Promise.resolve();
    });
    const addModule = vi.fn(() => new Promise<void>(() => {}));
    class FakeAudioContext {
      state: AudioContextState = "running";
      close = contextClose;
      audioWorklet = { addModule };
    }
    vi.stubGlobal("AudioContext", FakeAudioContext);
    const controller = new AbortController();

    const starting = DictationSession.start(events, { signal: controller.signal });
    await vi.waitFor(() => expect(addModule).toHaveBeenCalledOnce());
    controller.abort();

    await expect(starting).rejects.toMatchObject({ name: "AbortError" });
    expect(wsClose).toHaveBeenCalledOnce();
    expect(contextClose).toHaveBeenCalledOnce();
    expect(stop).toHaveBeenCalledOnce();
  });
});

describe("DictationSession lifecycle", () => {
  it("ignores socket events after cancel", () => {
    const events = { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() };
    const ws = {
      readyState: WebSocket.OPEN,
      close: vi.fn(),
      send: vi.fn(),
      onmessage: null,
      onclose: null,
    } as unknown as WebSocket;
    const stream = { getTracks: () => [] } as unknown as MediaStream;
    const context = { state: "running", close: vi.fn() } as unknown as AudioContext;
    const worklet = { port: { onmessage: null } } as unknown as AudioWorkletNode;
    const session = Reflect.construct(DictationSession, [events, ws, stream, context, worklet]) as {
      cancel: () => void;
    };

    session.cancel();
    (ws.onmessage as ((event: MessageEvent) => void) | null)?.({
      data: '{"type":"error","message":"late"}',
    } as MessageEvent);

    expect(events.onError).not.toHaveBeenCalled();
  });

  it("resolves a pending stop immediately when the server reports an error", async () => {
    const events = { onPartial: vi.fn(), onFinal: vi.fn(), onError: vi.fn() };
    const ws = {
      readyState: WebSocket.OPEN,
      close: vi.fn(),
      send: vi.fn(),
      onmessage: null,
      onclose: null,
    } as unknown as WebSocket;
    const stream = { getTracks: () => [] } as unknown as MediaStream;
    const context = { state: "running", close: vi.fn() } as unknown as AudioContext;
    const port = {
      onmessage: null as ((event: MessageEvent<Int16Array<ArrayBuffer> | null>) => void) | null,
      postMessage: vi.fn(() =>
        queueMicrotask(() => port.onmessage?.({ data: null } as MessageEvent)),
      ),
    };
    const worklet = { port } as unknown as AudioWorkletNode;
    const session = Reflect.construct(DictationSession, [events, ws, stream, context, worklet]) as {
      stop: () => Promise<string>;
    };

    const stopping = session.stop();
    await vi.waitFor(() => expect(ws.send).toHaveBeenCalled());
    (ws.onmessage as ((event: MessageEvent) => void) | null)?.({
      data: '{"type":"error","message":"worker failed"}',
    } as MessageEvent);

    await expect(stopping).resolves.toBe("");
    expect(events.onError).toHaveBeenCalledWith("worker failed");
  });
});
