import { describe, expect, it } from "vitest";
import {
  allowedVideoType,
  readVideoResponse,
  UnsupportedVideoTypeError,
  VideoTooLargeError,
} from "./videoFetch";

const LIMIT = 100;

/** A response streaming `chunks`, recording how much was pulled and whether it was cancelled. */
function streamingResponse(chunks: number[], headers: Record<string, string> = {}) {
  const state = { pulled: 0, cancelled: false };
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      const size = chunks[index++];
      state.pulled += size;
      controller.enqueue(new Uint8Array(size));
    },
    cancel() {
      state.cancelled = true;
    },
  });
  const response = new Response(body, {
    headers: { "content-type": "video/mp4", ...headers },
  });
  return { response, state };
}

describe("allowedVideoType", () => {
  it.each([
    ["video/mp4", "a.mp4", "video/mp4"],
    ["Video/MP4; codecs=avc1", "a.bin", "video/mp4"],
    ["video/quicktime", "a.mov", "video/quicktime"],
    [null, "clips/demo.webm", "video/webm"],
    ["application/octet-stream", "demo.MOV", "video/quicktime"],
    // Paths are literal filenames: `?` and `#` are part of the name, not a URL suffix.
    ["application/octet-stream", "take#2.m4v", "video/x-m4v"],
    [null, "what?.mp4", "video/mp4"],
    ["application/octet-stream", "demo.m4v?x=1", null],
    [null, "clip.mp4#t=1", null],
    ["application/octet-stream", "clips.mp4/notes", null],
    ["application/octet-stream", "mp4", null],
    ["text/html", "demo.mp4", null],
    ["video/mp2t", "demo.ts", null],
    [null, "demo.avi", null],
    ["application/octet-stream", "notes", null],
  ])("%s for %s is %s", (contentType, path, expected) => {
    expect(allowedVideoType(contentType, path)).toBe(expected);
  });

  // Extensions named like Object.prototype members aren't video types.
  it.each([
    [null, "clip.constructor"],
    ["application/octet-stream", "clip.__proto__"],
    ["application/octet-stream", "constructor"],
  ])("%s for %s is not allowed", async (contentType, path) => {
    expect(allowedVideoType(contentType, path)).toBeNull();
    const headers: Record<string, string> = contentType ? { "content-type": contentType } : {};
    const response = new Response(new Uint8Array(4), { headers });
    if (!contentType) response.headers.delete("content-type");
    await expect(readVideoResponse(response, path)).rejects.toBeInstanceOf(
      UnsupportedVideoTypeError,
    );
  });
});

describe("readVideoResponse", () => {
  it("rejects a declared length over the limit without reading, and cancels the body", async () => {
    const { response, state } = streamingResponse([10], { "content-length": "101" });
    await expect(
      readVideoResponse(response, "a.mp4", { limitBytes: LIMIT }),
    ).rejects.toBeInstanceOf(VideoTooLargeError);
    expect(state.pulled).toBe(0);
    expect(state.cancelled).toBe(true);
  });

  it("aborts a response with no length once it crosses the limit", async () => {
    const { response, state } = streamingResponse([60, 60, 60]);
    await expect(
      readVideoResponse(response, "a.mp4", { limitBytes: LIMIT }),
    ).rejects.toBeInstanceOf(VideoTooLargeError);
    expect(state.cancelled).toBe(true);
    expect(state.pulled).toBeLessThan(180);
  });

  it("counts bytes even when the declared length is understated", async () => {
    const { response, state } = streamingResponse([50, 50, 50], { "content-length": "10" });
    await expect(
      readVideoResponse(response, "a.mp4", { limitBytes: LIMIT }),
    ).rejects.toBeInstanceOf(VideoTooLargeError);
    expect(state.cancelled).toBe(true);
  });

  it("accepts exactly the limit and refuses one byte more", async () => {
    const exact = streamingResponse([40, 60]);
    const blob = await readVideoResponse(exact.response, "a.mp4", { limitBytes: LIMIT });
    expect(blob.size).toBe(100);
    expect(blob.type).toBe("video/mp4");

    const over = streamingResponse([40, 61]);
    await expect(
      readVideoResponse(over.response, "a.mp4", { limitBytes: LIMIT }),
    ).rejects.toBeInstanceOf(VideoTooLargeError);
  });

  it("types the blob from the allowlist, and rejects other types with the body cancelled", async () => {
    const octet = streamingResponse([5], { "content-type": "application/octet-stream" });
    expect((await readVideoResponse(octet.response, "clip.webm")).type).toBe("video/webm");

    const html = streamingResponse([5], { "content-type": "text/html" });
    await expect(readVideoResponse(html.response, "clip.mp4")).rejects.toBeInstanceOf(
      UnsupportedVideoTypeError,
    );
    expect(html.state.pulled).toBe(0);
    expect(html.state.cancelled).toBe(true);
  });

  it("rejects over the limit at once, even when the body's cancel never settles", async () => {
    let cancelRequested = false;
    const body = new ReadableStream<Uint8Array>({
      pull(stream) {
        stream.enqueue(new Uint8Array(40));
      },
      cancel() {
        cancelRequested = true;
        return new Promise<void>(() => {});
      },
    });
    const response = new Response(body, { headers: { "content-type": "video/mp4" } });
    const hung = new Promise((resolve) => {
      setTimeout(() => resolve("hung"), 200);
    });
    const outcome = await Promise.race([
      readVideoResponse(response, "a.mp4", { limitBytes: LIMIT }).catch((error: unknown) => error),
      hung,
    ]);
    expect(outcome).toBeInstanceOf(VideoTooLargeError);
    expect(cancelRequested).toBe(true);
  });

  it("cancels the body when aborted mid-stream", async () => {
    const controller = new AbortController();
    let pulls = 0;
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      pull(stream) {
        pulls += 1;
        if (pulls === 2) controller.abort();
        stream.enqueue(new Uint8Array(1));
      },
      cancel() {
        cancelled = true;
      },
    });
    const response = new Response(body, { headers: { "content-type": "video/mp4" } });
    await expect(
      readVideoResponse(response, "a.mp4", { signal: controller.signal, limitBytes: LIMIT }),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(cancelled).toBe(true);
  });

  it("rejects at once when aborted during a pending read whose cancel never settles", async () => {
    const controller = new AbortController();
    let readPending = false;
    let cancelRequested = false;
    const body = new ReadableStream<Uint8Array>(
      {
        pull() {
          // The read never gets a chunk, so only the abort can end it.
          readPending = true;
          return new Promise<void>(() => {});
        },
        cancel() {
          cancelRequested = true;
          return new Promise<void>(() => {});
        },
      },
      { highWaterMark: 0 },
    );
    const response = new Response(body, { headers: { "content-type": "video/mp4" } });
    const read = readVideoResponse(response, "a.mp4", {
      signal: controller.signal,
      limitBytes: LIMIT,
    }).catch((error: unknown) => error);
    await expect.poll(() => readPending).toBe(true);
    controller.abort();
    const hung = new Promise((resolve) => {
      setTimeout(() => resolve("hung"), 200);
    });
    const outcome = await Promise.race([read, hung]);
    expect(outcome).toMatchObject({ name: "AbortError" });
    expect(cancelRequested).toBe(true);
  });
});
