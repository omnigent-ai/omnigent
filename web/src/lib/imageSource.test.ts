import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  classifyImage,
  ImagePreviewError,
  loadImageSource,
  mimeEssence,
  SVG_DECODE_TIMEOUT_MS,
  SVG_INPUT_LIMIT_BYTES,
  SVG_MAX_CANVAS_SIDE,
  SVG_PIXEL_BUDGET,
  svgRasterSize,
} from "./imageSource";

describe("classifyImage", () => {
  it.each([
    ["image/png", "a.png", { kind: "raster", type: "image/png" }],
    ["Image/PNG; charset=binary", "a.png", { kind: "raster", type: "image/png" }],
    ["image/jpeg", "photo.jpg", { kind: "raster", type: "image/jpeg" }],
    [
      "image/vnd.microsoft.icon",
      "favicon.ico",
      { kind: "raster", type: "image/vnd.microsoft.icon" },
    ],
    // The declared raster type decides, so the bytes open only ever as that type.
    ["image/png", "drawing.svg", { kind: "raster", type: "image/png" }],
    ["image/png", "notes.html", { kind: "raster", type: "image/png" }],
    [null, "shot.png", { kind: "raster", type: "image/png" }],
    ["application/octet-stream", "shot.JPEG", { kind: "raster", type: "image/jpeg" }],
    ["image/svg+xml", "logo.svg", { kind: "svg" }],
    ["IMAGE/SVG+XML; charset=utf-8", "logo", { kind: "svg" }],
    ["image/svg+xml", "logo.png", { kind: "svg" }],
    [null, "logo.svg", { kind: "svg" }],
    ["application/octet-stream", "logo.svg", { kind: "svg" }],
    ["text/xml", "logo.svg", { kind: "svg" }],
    ["application/xml", "logo.svg", { kind: "svg" }],
    ["text/plain", "logo.SVG", { kind: "svg" }],
  ] as const)("shows %s named %s as %o", (type, path, expected) => {
    expect(classifyImage(type, path)).toEqual(expected);
  });

  it.each([
    ["text/html", "page.html"],
    ["text/html", "shot.png"],
    ["text/html", "logo.svg"],
    ["application/xml", "feed.xml"],
    ["application/xhtml+xml", "logo.svg"],
    ["image/heic", "photo.heic"],
    ["image/x-unknown", "a.png"],
    ["text/plain", "a.png"],
    [null, "README"],
    [null, undefined],
    ["", "archive.zip"],
  ])("refuses %s named %s", (type, path) => {
    expect(classifyImage(type, path)).toEqual({ kind: "unsupported" });
  });

  it.each([
    [null, "picture.constructor"],
    ["application/octet-stream", "picture.__proto__"],
    ["", "picture.CONSTRUCTOR"],
    [null, "picture.unknownext"],
  ])("refuses untyped %s bytes named %s, whose extension is no raster type", (type, path) => {
    expect(classifyImage(type, path)).toEqual({ kind: "unsupported" });
  });

  it("strips parameters and case from MIME types", () => {
    expect(mimeEssence(" Image/SVG+XML ; charset=UTF-8")).toBe("image/svg+xml");
    expect(mimeEssence(undefined)).toBe("");
  });
});

describe("svgRasterSize", () => {
  it("draws at the requested scale within the pixel budget, keeping the aspect ratio", () => {
    expect(svgRasterSize(100, 50, 2)).toEqual({ width: 200, height: 100 });
    const huge = svgRasterSize(20_000, 10_000, 1)!;
    expect(huge.width * huge.height).toBeLessThanOrEqual(SVG_PIXEL_BUDGET);
    expect(huge.width / huge.height).toBeCloseTo(2, 2);
  });

  it.each([
    [100_000_000, 1],
    [1, 100_000_000],
    [50_000, 3],
    [3, 50_000],
    [16_385, 16_385],
  ])("keeps a %d × %d SVG within the budget and the canvas side limit", (width, height) => {
    for (const scale of [0.5, 1, 2, 8]) {
      const size = svgRasterSize(width, height, scale)!;
      expect(size.width * size.height).toBeLessThanOrEqual(SVG_PIXEL_BUDGET);
      expect(Math.max(size.width, size.height)).toBeLessThanOrEqual(SVG_MAX_CANVAS_SIDE);
      expect(Math.min(size.width, size.height)).toBeGreaterThanOrEqual(1);
    }
  });

  it("refuses a size when rounding up a sub-pixel side would break the budget", () => {
    // A 1000 × 0.001 SVG drawn 1 pixel tall needs 1000 pixels.
    expect(svgRasterSize(1000, 0.001, 1, 1000)).toEqual({ width: 1000, height: 1 });
    expect(svgRasterSize(1000, 0.001, 1, 999)).toBeNull();
    expect(svgRasterSize(1, 1, 1, 0.5)).toBeNull();
  });

  it.each([
    [0, 10],
    [10, -1],
    [Number.NaN, 10],
    [10, Number.POSITIVE_INFINITY],
  ])("refuses a %d × %d SVG", (width, height) => {
    expect(svgRasterSize(width, height, 1)).toBeNull();
  });

  it("treats an unusable scale as 1", () => {
    expect(svgRasterSize(10, 10, Number.NaN)).toEqual({ width: 10, height: 10 });
    expect(svgRasterSize(10, 10, 0)).toEqual({ width: 10, height: 10 });
  });
});

/** Stand-ins for the browser's image decoder and canvas, which jsdom lacks. */
function fakeImaging({
  decode = "ok",
  width = 40,
  height = 20,
  exported = "png",
}: {
  decode?: "ok" | "error" | "hang";
  width?: number;
  height?: number;
  exported?: "png" | "tainted" | "hang";
} = {}) {
  const images: { src: string }[] = [];
  const created: { url: string; blob: Blob }[] = [];
  const revoked: string[] = [];
  const drawn: { width: number; height: number }[] = [];
  vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
    const url = `blob:test-${created.length + 1}`;
    created.push({ url, blob: blob as Blob });
    return url;
  });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation((url) => void revoked.push(url));
  class FakeImage {
    constructor() {
      images.push(this);
    }
    src = "";
    decoding = "auto";
    naturalWidth = width;
    naturalHeight = height;
    decode(): Promise<void> {
      if (decode === "error") return Promise.reject(new Error("bad svg"));
      if (decode === "hang") {
        return new Promise(() => {});
      }
      return Promise.resolve();
    }
  }
  vi.stubGlobal("Image", FakeImage);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (
    this: HTMLCanvasElement,
  ) {
    return {
      drawImage: (
        _image: unknown,
        _x: number,
        _y: number,
        drawnWidth: number,
        drawnHeight: number,
      ) => drawn.push({ width: drawnWidth, height: drawnHeight }),
    } as unknown as CanvasRenderingContext2D;
  } as never);
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
    if (exported === "tainted") throw new DOMException("tainted", "SecurityError");
    if (exported === "hang") return;
    callback(new Blob(["png"], { type: "image/png" }));
  });
  return { created, revoked, drawn, images };
}

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"/>';

describe("loadImageSource", () => {
  beforeEach(() => {
    vi.stubGlobal("devicePixelRatio", 2);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("passes an allowlisted raster through, retyped", async () => {
    const imaging = fakeImaging();
    const image = await loadImageSource(new Blob(["png"]), { path: "a.png" });
    expect(image).toMatchObject({ url: "blob:test-1", kind: "raster" });
    expect(imaging.created[0]!.blob.type).toBe("image/png");
  });

  it("draws an SVG into a PNG, and only the PNG's URL survives", async () => {
    const imaging = fakeImaging();
    const image = await loadImageSource(new Blob([SVG], { type: "image/svg+xml" }));
    expect(imaging.created.map((entry) => entry.blob.type)).toEqual(["image/svg+xml", "image/png"]);
    // The SVG's own URL was only for decoding off the page.
    expect(imaging.revoked).toEqual(["blob:test-1"]);
    expect(image).toMatchObject({ url: "blob:test-2", kind: "svg", width: 40, height: 20 });
    expect(imaging.drawn).toEqual([{ width: 80, height: 40 }]);
    expect(image).toMatchObject({ pixelWidth: 80, pixelHeight: 40 });
  });

  it.each([
    ["a decode error", { decode: "error" } as const, "undecodable"],
    ["no intrinsic size", { width: 0 } as const, "undecodable"],
    ["a tainted canvas", { exported: "tainted" } as const, "unsafe"],
  ])("fails closed on %s, leaving no URL", async (_name, imaging, problem) => {
    const fake = fakeImaging(imaging);
    const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }));
    await expect(loading).rejects.toMatchObject({ problem });
    expect(fake.revoked).toEqual(fake.created.map((entry) => entry.url));
    expect(fake.created.some((entry) => entry.blob.type === "image/png")).toBe(false);
  });

  it("refuses an SVG over the input limit without decoding it", async () => {
    const imaging = fakeImaging();
    const big = new Blob([new Uint8Array(SVG_INPUT_LIMIT_BYTES + 1)], { type: "image/svg+xml" });
    await expect(loadImageSource(big)).rejects.toMatchObject({ problem: "too-large" });
    expect(imaging.created).toEqual([]);
  });

  it("refuses types off the allowlist without making a URL", async () => {
    const imaging = fakeImaging();
    const html = new Blob(["<script>top.ran=1</script>"], { type: "text/html" });
    const error = await loadImageSource(html, { path: "x.png" }).catch((caught) => caught);
    expect(error).toBeInstanceOf(ImagePreviewError);
    expect(error.problem).toBe("unsupported");
    expect(imaging.created).toEqual([]);
  });

  it("prefers the given type to the blob's own", async () => {
    const imaging = fakeImaging();
    await loadImageSource(new Blob([SVG], { type: "image/png" }), { type: "image/svg+xml" });
    expect(imaging.created.at(-1)!.blob.type).toBe("image/png");
    expect(imaging.drawn).toHaveLength(1);
  });

  it("an abort while decoding settles at once and revokes the decode's URL", async () => {
    // The decode never settles, so only the abort can end the load.
    const imaging = fakeImaging({ decode: "hang" });
    const controller = new AbortController();
    const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }), {
      signal: controller.signal,
    });
    controller.abort();
    await expect(loading).rejects.toMatchObject({ name: "AbortError" });
    expect(imaging.revoked).toEqual(imaging.created.map((entry) => entry.url));
    expect(imaging.created).toHaveLength(1);
    expect(imaging.images.map((image) => image.src)).toEqual([""]);
  });

  it("an abort while exporting the PNG settles at once and leaves no URL", async () => {
    const imaging = fakeImaging({ exported: "hang" });
    const controller = new AbortController();
    const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }), {
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(imaging.drawn).toHaveLength(1));
    controller.abort();
    await expect(loading).rejects.toMatchObject({ name: "AbortError" });
    expect(imaging.revoked).toEqual(imaging.created.map((entry) => entry.url));
  });

  it.each([
    ["decoding", { decode: "hang" }],
    ["exporting its PNG", { exported: "hang" }],
  ] as const)(
    "gives up on an SVG stuck %s once the deadline passes, and leaves no URL",
    async (_stage, stuck) => {
      vi.useFakeTimers();
      try {
        const imaging = fakeImaging(stuck);
        const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }));
        const settled = expect(loading).rejects.toMatchObject({
          name: "ImagePreviewError",
          problem: "undecodable",
          message: "This SVG couldn't be decoded.",
        });
        await vi.advanceTimersByTimeAsync(SVG_DECODE_TIMEOUT_MS - 1);
        expect(imaging.revoked).toEqual([]);
        await vi.advanceTimersByTimeAsync(1);
        await settled;
        // The decode's URL is revoked and no PNG URL was ever made.
        expect(imaging.created).toHaveLength(1);
        expect(imaging.revoked).toEqual(imaging.created.map((entry) => entry.url));
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("still reports a caller's abort as an abort, not as a timeout", async () => {
    vi.useFakeTimers();
    try {
      fakeImaging({ decode: "hang" });
      const controller = new AbortController();
      const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }), {
        signal: controller.signal,
      });
      controller.abort();
      await expect(loading).rejects.toMatchObject({ name: "AbortError" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("refuses an SVG with no size it can be drawn at within the limits", async () => {
    const imaging = fakeImaging({ width: Number.POSITIVE_INFINITY, height: 10 });
    const loading = loadImageSource(new Blob([SVG], { type: "image/svg+xml" }));
    await expect(loading).rejects.toMatchObject({ problem: "too-large" });
    expect(imaging.drawn).toEqual([]);
    expect(imaging.revoked).toEqual(imaging.created.map((entry) => entry.url));
  });
});

describe("file reads from every route", () => {
  // The shell's helper and the runner can encode the same SVG differently.
  it("gives an SVG read as UTF-8 or as base64 the same bytes and handling", async () => {
    const { fileContentToBlob } = await import("@/hooks/useFileContent");
    const read = {
      object: "session.environment.filesystem.file_content",
      path: "a.svg",
      bytes: SVG.length,
    } as const;
    const asText = fileContentToBlob({
      ...read,
      content_type: "image/svg+xml",
      encoding: "utf-8",
      content: SVG,
    });
    const asBase64 = fileContentToBlob({
      ...read,
      content_type: "image/svg+xml",
      encoding: "base64",
      content: btoa(SVG),
    });
    expect(await asText.text()).toBe(await asBase64.text());
    expect(classifyImage(asText.type, "a.svg")).toEqual(classifyImage(asBase64.type, "a.svg"));
    expect(classifyImage(asText.type, "a.svg")).toEqual({ kind: "svg" });
  });
});
