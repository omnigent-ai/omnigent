// Image attachments: HEIC/HEIF photos are converted to JPEG (the model and
// the server accept neither), and any image over the upload cap is
// downscaled to a 2048 px long edge and re-encoded down a quality ladder
// until it fits. Small images pass through untouched so PNG transparency and
// exact pixels survive.
//
// Ported from T3 Code (github.com/pingdotgg/t3code, MIT) —
// apps/web/src/lib/imageCompression.ts.

import { ATTACHMENT_SIZE_LIMITS_MB } from "./attachments";

export const MAX_IMAGE_DIMENSION = 2048;
export const MAX_IMAGE_UPLOAD_BYTES = ATTACHMENT_SIZE_LIMITS_MB.image * 1024 * 1024;
/** Sources larger than this are refused outright; decoding them is the risk. */
export const MAX_COMPRESSIBLE_SOURCE_BYTES = 50 * 1024 * 1024;
// Quality ladder: visually lossless first, then progressively smaller. If even
// the lowest quality overflows, resolution drops instead.
const QUALITY_STEPS = [0.92, 0.85, 0.78, 0.68] as const;
const FALLBACK_SCALE_STEPS = [0.75, 0.55] as const;
const HEIC_IMAGE_MIME_TYPE = /^image\/hei(?:c|f)$/i;
const HEIC_IMAGE_EXTENSION = /\.(?:heic|heif)$/i;

export type ImageCompressionFailureReason = "too-large" | "unreadable";

export type PreparedAttachment =
  | { ok: true; file: File; converted: boolean }
  | { ok: false; reason: ImageCompressionFailureReason; file: File };

export function isHeicImageFile(file: Pick<File, "name" | "type">): boolean {
  return HEIC_IMAGE_MIME_TYPE.test(file.type) || HEIC_IMAGE_EXTENSION.test(file.name);
}

function canRecompress(): boolean {
  return (
    typeof createImageBitmap === "function" &&
    (typeof OffscreenCanvas === "function" || typeof document !== "undefined")
  );
}

interface Canvas2D {
  canvas: OffscreenCanvas | HTMLCanvasElement;
  context: OffscreenCanvasRenderingContext2D | CanvasRenderingContext2D;
}

function createCanvas(width: number, height: number): Canvas2D | null {
  if (typeof OffscreenCanvas === "function") {
    const canvas = new OffscreenCanvas(width, height);
    const context = canvas.getContext("2d");
    return context ? { canvas, context } : null;
  }
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  return context ? { canvas, context } : null;
}

function canvasToBlob(
  canvas: OffscreenCanvas | HTMLCanvasElement,
  mimeType: string,
  quality: number,
): Promise<Blob | null> {
  if (typeof HTMLCanvasElement !== "undefined" && canvas instanceof HTMLCanvasElement) {
    return new Promise((resolve) => {
      canvas.toBlob(
        (blob) => resolve(blob && blob.type === mimeType ? blob : null),
        mimeType,
        quality,
      );
    });
  }
  return (canvas as OffscreenCanvas)
    .convertToBlob({ type: mimeType, quality })
    .then((blob) => (blob.type && blob.type !== mimeType ? null : blob))
    .catch(() => null);
}

/** Draw `bitmap` at most `maxDimension` wide/high and encode within `budgetBytes`. */
async function encodeWithinBudget(
  bitmap: ImageBitmap,
  maxDimension: number,
  budgetBytes: number,
  mimeType: "image/jpeg" | "image/webp",
): Promise<Blob | null> {
  const scale = Math.min(1, maxDimension / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const target = createCanvas(width, height);
  if (!target) return null;
  if (mimeType === "image/jpeg") {
    // JPEG has no alpha; matte transparent regions on white.
    target.context.fillStyle = "#ffffff";
    target.context.fillRect(0, 0, width, height);
  }
  target.context.drawImage(bitmap, 0, 0, width, height);
  for (const quality of QUALITY_STEPS) {
    // oxlint-disable-next-line no-await-in-loop
    const blob = await canvasToBlob(target.canvas, mimeType, quality);
    if (blob === null) return null;
    if (blob.size <= budgetBytes) return blob;
  }
  return null;
}

async function decodeBitmap(file: File): Promise<ImageBitmap | null> {
  try {
    return await createImageBitmap(file);
  } catch {
    // Chromium cannot decode HEIC; hand the bytes to the wasm decoder and
    // try again from the JPEG it produces.
    if (!isHeicImageFile(file)) return null;
    try {
      const { default: heic2any } = await import("heic2any");
      const converted = await heic2any({ blob: file, toType: "image/jpeg", quality: 0.92 });
      const blob = Array.isArray(converted) ? converted[0] : converted;
      return blob ? await createImageBitmap(blob) : null;
    } catch {
      return null;
    }
  }
}

function replaceExtension(name: string, extension: string): string {
  const base = name.replace(/\.[^.]+$/, "") || "image";
  return `${base}.${extension}`;
}

/**
 * Decode `file`, then walk the quality ladder and fallback downscale passes
 * until an encoding fits `budgetBytes`. WebP is preferred (smaller at the
 * same quality, keeps alpha); browsers that cannot encode it fall back to
 * JPEG. HEIC sources always land as JPEG.
 */
async function reencodeWithinBudget(
  file: File,
  budgetBytes: number,
  maxDimension: number,
): Promise<PreparedAttachment> {
  if (!canRecompress()) return { ok: false, reason: "too-large", file };
  const bitmap = await decodeBitmap(file);
  if (bitmap === null) return { ok: false, reason: "unreadable", file };
  try {
    const heic = isHeicImageFile(file);
    const preferred: ("image/jpeg" | "image/webp")[] = heic
      ? ["image/jpeg"]
      : ["image/webp", "image/jpeg"];
    const baseDimension = Math.min(maxDimension, Math.max(bitmap.width, bitmap.height));
    let encodeFailed = false;
    for (const dimensionScale of [1, ...FALLBACK_SCALE_STEPS]) {
      const targetDimension = Math.max(1, Math.round(baseDimension * dimensionScale));
      for (const mimeType of preferred) {
        let blob: Blob | null;
        try {
          // oxlint-disable-next-line no-await-in-loop
          blob = await encodeWithinBudget(bitmap, targetDimension, budgetBytes, mimeType);
        } catch {
          // Canvas allocation or the codec can throw on a huge bitmap; a
          // smaller pass may still succeed, so keep going.
          encodeFailed = true;
          continue;
        }
        encodeFailed = false;
        if (blob !== null) {
          const extension = mimeType === "image/webp" ? "webp" : "jpg";
          return {
            ok: true,
            converted: true,
            file: new File([blob], replaceExtension(file.name || "image", extension), {
              type: mimeType,
              lastModified: file.lastModified,
            }),
          };
        }
        // `null` from the encoder means the codec is unsupported; try the
        // next mime type before shrinking.
      }
    }
    return { ok: false, reason: encodeFailed ? "unreadable" : "too-large", file };
  } finally {
    bitmap.close();
  }
}

/**
 * Make an image ready to attach: convert HEIC/HEIF to JPEG, and shrink any
 * image over `budgetBytes` to fit. Non-images and images already within
 * budget pass through unchanged.
 */
export async function prepareImageAttachment(
  file: File,
  budgetBytes: number = MAX_IMAGE_UPLOAD_BYTES,
  maxDimension: number = MAX_IMAGE_DIMENSION,
): Promise<PreparedAttachment> {
  const isImage = file.type.startsWith("image/") || isHeicImageFile(file);
  if (!isImage) return { ok: true, file, converted: false };
  if (file.size > MAX_COMPRESSIBLE_SOURCE_BYTES) return { ok: false, reason: "too-large", file };
  if (!isHeicImageFile(file) && file.size <= budgetBytes) {
    return { ok: true, file, converted: false };
  }
  return reencodeWithinBudget(file, budgetBytes, maxDimension);
}

/** User-facing text for a failed preparation. */
export function describeImagePreparationFailure(
  name: string,
  reason: ImageCompressionFailureReason,
): string {
  return reason === "unreadable"
    ? `"${name}" couldn't be read as an image.`
    : `"${name}" is too large to shrink under the ${ATTACHMENT_SIZE_LIMITS_MB.image} MB image limit.`;
}
