import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MAX_IMAGE_UPLOAD_BYTES,
  describeImagePreparationFailure,
  isHeicImageFile,
  prepareImageAttachment,
} from "./imageCompression";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

vi.mock("heic2any", () => ({
  default: vi.fn(async () => {
    throw new Error("wasm decoder unavailable in jsdom");
  }),
}));

describe("prepareImageAttachment", () => {
  it("detects HEIC by mime or extension", () => {
    expect(isHeicImageFile({ name: "IMG_1.HEIC", type: "" })).toBe(true);
    expect(isHeicImageFile({ name: "photo", type: "image/heif" })).toBe(true);
    expect(isHeicImageFile({ name: "photo.png", type: "image/png" })).toBe(false);
  });

  it("passes non-images and every non-HEIC image through untouched", async () => {
    // The server re-encodes an oversized compressible raster image under the
    // provider's per-image limit, so the browser must not shrink it here.
    const pdf = new File(["%PDF"], "doc.pdf", { type: "application/pdf" });
    expect(await prepareImageAttachment(pdf)).toEqual({ ok: true, file: pdf, converted: false });
    const png = new File([new Uint8Array(1024)], "shot.png", { type: "image/png" });
    expect(await prepareImageAttachment(png)).toEqual({ ok: true, file: png, converted: false });
    const huge = new File([new Uint8Array(1024)], "huge.png", { type: "image/png" });
    Object.defineProperty(huge, "size", { value: MAX_IMAGE_UPLOAD_BYTES + 1 });
    expect(await prepareImageAttachment(huge)).toEqual({ ok: true, file: huge, converted: false });
  });

  it("decodes a HEIC photo and hands back the JPEG", async () => {
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

    const photo = new File([new Uint8Array(2048)], "photo.heic", { type: "image/heic" });
    const result = await prepareImageAttachment(photo);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.converted).toBe(true);
    expect(result.file.type).toBe("image/jpeg");
    expect(result.file.name).toBe("photo.jpg");
    // Long edge capped at 2048: the 4096x2048 source draws at 2048x1024.
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 2048, 1024);
    expect(close).toHaveBeenCalled();
  });

  it("reports an unreadable source when nothing can decode the HEIC", async () => {
    vi.stubGlobal(
      "createImageBitmap",
      vi.fn(async () => Promise.reject(new Error("nope"))),
    );
    const bad = new File([new Uint8Array(2048)], "bad.heic", { type: "image/heic" });
    const result = await prepareImageAttachment(bad);
    expect(result).toMatchObject({ ok: false, reason: "unreadable" });
    expect(describeImagePreparationFailure("bad.heic", "unreadable")).toContain("couldn't be read");
    expect(describeImagePreparationFailure("bad.heic", "too-large")).toContain("image limit");
  });
});
