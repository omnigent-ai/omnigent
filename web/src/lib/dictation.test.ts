// Tests for the dictation socket protocol parsing. The DictationSession
// transport itself (mic + AudioWorklet + WebSocket) can't run in jsdom;
// its behavior against the component is pinned in ComposerMicButton.test.tsx
// with a mocked session, and the full loop runs in the Playwright e2e test
// against the server's fake engine.

import { afterEach, describe, expect, it, vi } from "vitest";
import { getDictationMediaStream, parseDictationEvent } from "./dictation";

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
    "retries a missing exact device once with the default after %s",
    async (name) => {
      const stream = {} as MediaStream;
      const getUserMedia = vi
        .fn()
        .mockRejectedValueOnce(new DOMException("missing", name))
        .mockResolvedValueOnce(stream);
      installMediaDevices(getUserMedia);

      await expect(getDictationMediaStream("mic-2")).resolves.toBe(stream);
      expect(getUserMedia).toHaveBeenNthCalledWith(1, {
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          deviceId: { exact: "mic-2" },
        },
      });
      expect(getUserMedia).toHaveBeenNthCalledWith(2, {
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
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
