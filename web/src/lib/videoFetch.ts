// Reads a downloaded video into a Blob the player may show: only allowlisted
// video types, and never more than a fixed number of body bytes, whatever the
// response claims about its length.

/** The most response body bytes a video may have to play in the app. */
export const VIDEO_PAYLOAD_LIMIT_BYTES = 200 * 1024 * 1024;

/** Video types the player accepts, by MIME and by file extension. */
const VIDEO_TYPES = new Set([
  "video/mp4",
  "video/webm",
  "video/quicktime",
  "video/ogg",
  "video/x-m4v",
]);
// A Map, so names like "constructor" don't resolve to Object.prototype members.
const VIDEO_TYPE_BY_EXTENSION: ReadonlyMap<string, string> = new Map([
  ["mp4", "video/mp4"],
  ["m4v", "video/x-m4v"],
  ["webm", "video/webm"],
  ["mov", "video/quicktime"],
  ["ogv", "video/ogg"],
]);

/** The video is over the payload limit, by its declared length or by the bytes it sent. */
export class VideoTooLargeError extends Error {
  constructor() {
    super("This video is too large to play here.");
    this.name = "VideoTooLargeError";
  }
}

/** The response isn't one of the video types the player accepts. */
export class UnsupportedVideoTypeError extends Error {
  constructor(type: string) {
    super(`Unsupported video type: ${type || "unknown"}`);
    this.name = "UnsupportedVideoTypeError";
  }
}

/**
 * The allowlisted video type for a response, or null. A missing or
 * octet-stream type falls back to the extension of the path's basename. The
 * path is a literal filesystem path, so `?` and `#` are part of the name.
 */
export function allowedVideoType(contentType: string | null, path: string): string | null {
  const type = contentType?.split(";")[0].trim().toLowerCase() ?? "";
  if (type && type !== "application/octet-stream") return VIDEO_TYPES.has(type) ? type : null;
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  const extension = dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
  return VIDEO_TYPE_BY_EXTENSION.get(extension) ?? null;
}

function abortError(): DOMException {
  return new DOMException("The video download was cancelled.", "AbortError");
}

function cancelBody(response: Response): void {
  void response.body?.cancel().catch(() => {});
}

/**
 * The response body as a Blob typed with its allowlisted video type. Rejects,
 * cancelling the body, on a disallowed type, a declared length over
 * `limitBytes`, more than `limitBytes` received, or `signal` aborting.
 */
export async function readVideoResponse(
  response: Response,
  path: string,
  {
    signal,
    limitBytes = VIDEO_PAYLOAD_LIMIT_BYTES,
  }: { signal?: AbortSignal; limitBytes?: number } = {},
): Promise<Blob> {
  const contentType = response.headers.get("content-type");
  const type = allowedVideoType(contentType, path);
  if (!type) {
    cancelBody(response);
    throw new UnsupportedVideoTypeError(contentType ?? "");
  }
  const declared = Number(response.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > limitBytes) {
    cancelBody(response);
    throw new VideoTooLargeError();
  }
  if (signal?.aborted) {
    cancelBody(response);
    throw abortError();
  }
  const reader = response.body?.getReader();
  if (!reader) throw new UnsupportedVideoTypeError(contentType ?? "");

  // Count every response, since a declared length can be missing, understated, or encoded.
  const onAbort = () => void reader.cancel().catch(() => {});
  signal?.addEventListener("abort", onAbort, { once: true });
  const chunks: Uint8Array[] = [];
  let received = 0;
  try {
    // Each read advances the same stream reader.
    /* oxlint-disable no-await-in-loop */
    for (;;) {
      const { done, value } = await reader.read();
      if (signal?.aborted) throw abortError();
      if (done) break;
      received += value.byteLength;
      if (received > limitBytes) {
        // Reject now: a body whose cancel never settles must not hold the error back.
        void reader.cancel().catch(() => {});
        throw new VideoTooLargeError();
      }
      chunks.push(value);
    }
    /* oxlint-enable no-await-in-loop */
  } finally {
    signal?.removeEventListener("abort", onAbort);
  }
  return new Blob(chunks as BlobPart[], { type });
}
