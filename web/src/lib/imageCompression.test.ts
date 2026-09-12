import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MAX_IMAGE_UPLOAD_BYTES,
  describeImagePreparationFailure,
  isHeicImageFile,
  prepareImageAttachment,
} from "./imageCompression";

afterEach(() => vi.unstubAllGlobals());

describe("prepareImageAttachment", () => {
  it("detects HEIC by mime or extension", () => {
    expect(isHeicImageFile({ name: "IMG_1.HEIC", type: "" })).toBe(true);
    expect(isHeicImageFile({ name: "photo", type: "image/heif" })).toBe(true);
    expect(isHeicImageFile({ name: "photo.png", type: "image/png" })).toBe(false);
  });

  it("passes non-images and in-budget images through untouched", async () => {
    const pdf = new File(["%PDF"], "doc.pdf", { type: "application/pdf" });
    expect(await prepareImageAttachment(pdf)).toEqual({ ok: true, file: pdf, converted: false });
    const png = new File([new Uint8Array(1024)], "shot.png", { type: "image/png" });
    expect(await prepareImageAttachment(png)).toEqual({ ok: true, file: png, converted: false });
  });

  it("re-encodes an oversized image through a canvas and reports the new file", async () => {
    // jsdom has no codecs: stub the decode + canvas pieces the ladder uses.
    const close = vi.fn();
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => ({ width: 4096, height: 2048, close })),
    );
    const toBlob = vi.fn((cb: (blob: Blob | null) => void, type: string) =>
      cb(new Blob([new Uint8Array(1000)], { type })),
    );
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
      () =>
        ({ drawImage, fillRect: vi.fn(), fillStyle: "" }) as unknown as CanvasRenderingContext2D,
    );
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation(toBlob as never);

    const big = new File([new Uint8Array(MAX_IMAGE_UPLOAD_BYTES + 1)], "huge.png", {
      type: "image/png",
    });
    const result = await prepareImageAttachment(big);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.converted).toBe(true);
    expect(result.file.type).toBe("image/webp");
    expect(result.file.name).toBe("huge.webp");
    // Long edge capped at 2048: the 4096x2048 source draws at 2048x1024.
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 2048, 1024);
    expect(close).toHaveBeenCalled();
  });

  it("reports an unreadable source when nothing can decode it", async () => {
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => Promise.reject(new Error("nope"))),
    );
    const bad = new File([new Uint8Array(MAX_IMAGE_UPLOAD_BYTES + 1)], "bad.png", {
      type: "image/png",
    });
    const result = await prepareImageAttachment(bad);
    expect(result).toMatchObject({ ok: false, reason: "unreadable" });
    expect(describeImagePreparationFailure("bad.png", "unreadable")).toContain("couldn't be read");
    expect(describeImagePreparationFailure("bad.png", "too-large")).toContain("5 MB");
  });
});
