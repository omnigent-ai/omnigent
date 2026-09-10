import { afterEach, describe, expect, it, vi } from "vitest";
import type { MessageContentBlock } from "./blocks";

const authenticatedFetch = vi.fn();

vi.mock("./identity", () => ({
  authenticatedFetch: (path: string) => authenticatedFetch(path),
}));

import { firstImageLoader, sessionFileContentPath } from "./attachmentClipboard";

// 1x1 PNG, inline exactly as an imported transcript carries it.
const INLINE_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

describe("sessionFileContentPath", () => {
  it("builds the file-content route, encoding both ids", () => {
    expect(sessionFileContentPath("conv a/b", "file c/d")).toBe(
      "/v1/sessions/conv%20a%2Fb/resources/files/file%20c%2Fd/content",
    );
  });
});

describe("firstImageLoader", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("loads an uploaded image via authenticatedFetch at its session file-content path", async () => {
    const blob = new Blob([], { type: "image/png" });
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });

    const loader = firstImageLoader(
      [{ type: "input_image", file_id: "file_1", filename: "shot.png" }],
      "conv_1",
    );

    expect(loader).not.toBeNull();
    const result = await loader!();
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_1/resources/files/file_1/content",
    );
    expect(result).toBe(blob);
  });

  it("loads an inline data-URI image without any fetch", async () => {
    const loader = firstImageLoader(
      [{ type: "input_image", image_url: INLINE_PNG, filename: "shot.png" }],
      null,
    );

    expect(loader).not.toBeNull();
    const blob = await loader!();
    expect(blob.type).toBe("image/png");
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("returns null for a pending upload", () => {
    const loader = firstImageLoader(
      [{ type: "input_image", file_id: "pending:shot.png" }],
      "conv_1",
    );
    expect(loader).toBeNull();
  });

  it("returns null for an image block with nothing renderable", () => {
    expect(firstImageLoader([{ type: "input_image" }], "conv_1")).toBeNull();
  });

  it("returns null when content has only non-image blocks", () => {
    const content: MessageContentBlock[] = [
      { type: "input_text", text: "notes attached" },
      { type: "input_file", file_id: "file_1", filename: "notes.pdf" },
    ];
    expect(firstImageLoader(content, "conv_1")).toBeNull();
  });

  it("returns null for an uploaded image when there is no session id", () => {
    expect(firstImageLoader([{ type: "input_image", file_id: "file_1" }], null)).toBeNull();
  });

  it("picks the first image when the message carries two", async () => {
    const loader = firstImageLoader(
      [
        { type: "input_image", image_url: INLINE_PNG, filename: "first.png" },
        { type: "input_image", file_id: "file_2", filename: "second.png" },
      ],
      "conv_1",
    );

    expect(loader).not.toBeNull();
    await loader!();
    // The first (inline) image resolved with no fetch; the second was never
    // even considered.
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("returns null when the first image has no session id, even though the second would resolve", () => {
    // Strict first-image semantics: a resolvable second image must never be
    // substituted for an unresolvable first one — that would silently copy a
    // different image than the one the user meant to copy.
    const loader = firstImageLoader(
      [
        { type: "input_image", file_id: "file_1", filename: "first.png" },
        { type: "input_image", image_url: INLINE_PNG, filename: "second.png" },
      ],
      null,
    );

    expect(loader).toBeNull();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("returns null when the first image is still pending, even though the second would resolve", () => {
    const loader = firstImageLoader(
      [
        { type: "input_image", file_id: "pending:first.png" },
        { type: "input_image", image_url: INLINE_PNG, filename: "second.png" },
      ],
      "conv_1",
    );

    expect(loader).toBeNull();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("rejects a non-PNG blob when there is no OffscreenCanvas to transcode with", async () => {
    // jsdom has neither createImageBitmap nor OffscreenCanvas, so the
    // transcode path itself is only reachable in a real browser (the e2e
    // suite covers it). This pins the rejection that copyTextWithImage's
    // text-only fallback depends on when transcoding isn't possible.
    const jpeg = new Blob([], { type: "image/jpeg" });
    authenticatedFetch.mockResolvedValue({ ok: true, blob: () => Promise.resolve(jpeg) });

    const loader = firstImageLoader(
      [{ type: "input_image", file_id: "file_1", filename: "shot.jpg" }],
      "conv_1",
    );

    await expect(loader!()).rejects.toThrow();
  });
});
