// Turns image bytes into a URL that is safe to hand to the DOM. Raster formats
// on an allowlist pass through, retyped; SVG is decoded in image mode, where it
// can't run script or load anything, and only a PNG of it is exposed, so a
// preview opened in a new tab is an inert picture. Anything else is refused.

/** Raster image types shown as they are. */
const RASTER_TYPES: ReadonlySet<string> = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
  "image/bmp",
  "image/x-icon",
  "image/vnd.microsoft.icon",
]);

/**
 * The raster type an extension implies when the bytes come untyped. A Map, so
 * names like "constructor" don't resolve to Object.prototype members.
 */
const RASTER_TYPE_BY_EXTENSION: ReadonlyMap<string, string> = new Map([
  ["png", "image/png"],
  ["jpg", "image/jpeg"],
  ["jpeg", "image/jpeg"],
  ["gif", "image/gif"],
  ["webp", "image/webp"],
  ["avif", "image/avif"],
  ["bmp", "image/bmp"],
  ["ico", "image/x-icon"],
]);

const SVG_TYPE = "image/svg+xml";
/** Types an `.svg` file commonly arrives with besides its own. */
const SVG_FILE_TYPES: ReadonlySet<string> = new Set([
  "",
  "application/octet-stream",
  "text/xml",
  "application/xml",
  "text/plain",
]);
const UNTYPED: ReadonlySet<string> = new Set(["", "application/octet-stream"]);

/** The largest SVG decoded for a preview, matching the file read limit. */
export const SVG_INPUT_LIMIT_BYTES = 10 * 1024 * 1024;
/** The most pixels one SVG preview is drawn with. */
export const SVG_PIXEL_BUDGET = 4096 * 4096;
/** The longest side a preview canvas is given, within every browser's canvas limit. */
export const SVG_MAX_CANVAS_SIDE = 16384;
/** How long an SVG gets to decode and draw before its preview gives up. */
export const SVG_DECODE_TIMEOUT_MS = 10_000;

export type ImageClassification =
  { kind: "raster"; type: string } | { kind: "svg" } | { kind: "unsupported" };

/** A MIME type without parameters, lowercased: `Image/PNG; x=1` is `image/png`. */
export function mimeEssence(type: string | null | undefined): string {
  return (type ?? "").split(";")[0]!.trim().toLowerCase();
}

function extensionOf(path: string | undefined): string {
  const name = path?.split(/[/\\]/).pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/**
 * How bytes with `type`, named `path`, may be shown. The declared type decides
 * when there is one; the extension only names untyped bytes. Bytes are never sniffed.
 */
export function classifyImage(type: string | null | undefined, path?: string): ImageClassification {
  const essence = mimeEssence(type);
  const extension = extensionOf(path);
  if (RASTER_TYPES.has(essence)) return { kind: "raster", type: essence };
  if (essence === SVG_TYPE) return { kind: "svg" };
  const rasterType = RASTER_TYPE_BY_EXTENSION.get(extension);
  if (rasterType && UNTYPED.has(essence)) return { kind: "raster", type: rasterType };
  if (extension === "svg" && SVG_FILE_TYPES.has(essence)) return { kind: "svg" };
  return { kind: "unsupported" };
}

export type ImagePreviewProblem = "unsupported" | "too-large" | "undecodable" | "unsafe";

/** Why an image can't be shown; no URL exists for it. */
export class ImagePreviewError extends Error {
  readonly problem: ImagePreviewProblem;

  constructor(problem: ImagePreviewProblem, message: string) {
    super(message);
    this.name = "ImagePreviewError";
    this.problem = problem;
  }
}

export interface LoadedImage {
  /** A blob URL of an allowlisted raster, or of a PNG drawn from SVG. */
  url: string;
  kind: "raster" | "svg";
  /** The SVG's own size in CSS pixels; 0 for rasters, whose `<img>` reports it. */
  width: number;
  height: number;
  /** The pixel size an SVG preview was drawn at; 0 for rasters. */
  pixelWidth: number;
  pixelHeight: number;
}

export interface LoadImageOptions {
  /** The file name, for untyped bytes. */
  path?: string;
  /** The type to use instead of the blob's own, such as a server's `content_type`. */
  type?: string | null;
  signal?: AbortSignal;
  /** Raster pixels per CSS pixel wanted for an SVG; the device pixel ratio by default. */
  rasterScale?: number;
}

function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError");
}

/**
 * The pixel size to draw a `width` × `height` SVG at, within the pixel budget
 * and the canvas side limit; null when no such size exists.
 */
export function svgRasterSize(
  width: number,
  height: number,
  rasterScale: number,
  pixelBudget: number = SVG_PIXEL_BUDGET,
): { width: number; height: number } | null {
  const positive = (value: number) => Number.isFinite(value) && value > 0;
  if (!positive(width) || !positive(height) || !positive(pixelBudget)) return null;
  const scale = Math.min(
    positive(rasterScale) ? rasterScale : 1,
    Math.sqrt(pixelBudget / (width * height)),
    SVG_MAX_CANVAS_SIDE / width,
    SVG_MAX_CANVAS_SIDE / height,
  );
  const size = {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  };
  // Rounding a sub-pixel side up to 1 must still respect both limits.
  if (size.width * size.height > pixelBudget) return null;
  if (size.width > SVG_MAX_CANVAS_SIDE || size.height > SVG_MAX_CANVAS_SIDE) return null;
  return size;
}

/** `promise`, or the signal's reason as soon as it aborts. */
function unlessAborted<T>(promise: Promise<T>, signal: AbortSignal | undefined): Promise<T> {
  if (!signal) return promise;
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

function canvasToPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    try {
      // A tainted canvas throws here; the caller fails closed.
      canvas.toBlob((png) => (png ? resolve(png) : reject(new Error("empty export"))), "image/png");
    } catch (error) {
      reject(error);
    }
  });
}

async function rasterizeSvg(blob: Blob, options: LoadImageOptions): Promise<LoadedImage> {
  if (blob.size > SVG_INPUT_LIMIT_BYTES) {
    throw new ImagePreviewError("too-large", "This SVG is too large to preview.");
  }
  // An SVG that never finishes decoding or drawing gives up rather than load forever. The
  // deadline aborts the draw, so its decode URL is revoked and a late PNG never gets a URL.
  const deadline = new AbortController();
  const caller = options.signal;
  const forwardAbort = () => deadline.abort(caller?.reason);
  if (caller?.aborted) forwardAbort();
  caller?.addEventListener("abort", forwardAbort, { once: true });
  const timer = setTimeout(() => deadline.abort(), SVG_DECODE_TIMEOUT_MS);
  try {
    return await drawSvg(blob, { ...options, signal: deadline.signal });
  } catch (error) {
    if (deadline.signal.aborted && !caller?.aborted) {
      throw new ImagePreviewError("undecodable", "This SVG couldn't be decoded.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
    caller?.removeEventListener("abort", forwardAbort);
  }
}

async function drawSvg(blob: Blob, options: LoadImageOptions): Promise<LoadedImage> {
  // Decoded off the page, so nothing can open or drag this SVG URL.
  const internalUrl = URL.createObjectURL(new Blob([blob], { type: SVG_TYPE }));
  const image = new Image();
  try {
    image.decoding = "async";
    image.src = internalUrl;
    try {
      // An abort settles at once, so the decode's URL is revoked without waiting for it.
      await unlessAborted(image.decode(), options.signal);
    } catch {
      throwIfAborted(options.signal);
      throw new ImagePreviewError("undecodable", "This SVG couldn't be decoded.");
    }
    const width = image.naturalWidth;
    const height = image.naturalHeight;
    if (!width || !height) {
      throw new ImagePreviewError("undecodable", "This SVG has no size to draw it at.");
    }
    const rasterScale = options.rasterScale ?? globalThis.devicePixelRatio ?? 1;
    const size = svgRasterSize(width, height, rasterScale);
    if (!size) throw new ImagePreviewError("too-large", "This SVG is too large to preview.");
    const canvas = document.createElement("canvas");
    canvas.width = size.width;
    canvas.height = size.height;
    const context = canvas.getContext("2d");
    if (!context) throw new ImagePreviewError("undecodable", "This SVG couldn't be drawn.");
    context.drawImage(image, 0, 0, size.width, size.height);
    let png: Blob;
    try {
      png = await unlessAborted(canvasToPng(canvas), options.signal);
    } catch {
      throwIfAborted(options.signal);
      throw new ImagePreviewError("unsafe", "This SVG couldn't be previewed safely.");
    }
    throwIfAborted(options.signal);
    return {
      url: URL.createObjectURL(png),
      kind: "svg",
      width,
      height,
      pixelWidth: size.width,
      pixelHeight: size.height,
    };
  } finally {
    image.src = "";
    URL.revokeObjectURL(internalUrl);
  }
}

/**
 * A URL that shows `blob` as an image, owned by the caller, who revokes it.
 * Throws {@link ImagePreviewError} when it can't be shown safely, and the
 * signal's reason when aborted; neither leaves a URL behind.
 */
export async function loadImageSource(
  blob: Blob,
  options: LoadImageOptions = {},
): Promise<LoadedImage> {
  throwIfAborted(options.signal);
  const classification = classifyImage(options.type ?? blob.type, options.path);
  if (classification.kind === "unsupported") {
    throw new ImagePreviewError("unsupported", "This file type can't be previewed.");
  }
  if (classification.kind === "svg") return rasterizeSvg(blob, options);
  // Retyped, so the URL opened as a page is always shown as this image type.
  const typed = new Blob([blob], { type: classification.type });
  return {
    url: URL.createObjectURL(typed),
    kind: "raster",
    width: 0,
    height: 0,
    pixelWidth: 0,
    pixelHeight: 0,
  };
}
