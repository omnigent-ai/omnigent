// Resolves the first copyable image in a user message's content blocks into a
// loader the clipboard can pull bytes from, for "copy message" (see
// components/chat/chatBubbleParts.tsx and lib/clipboard.ts's copyTextWithImage).

import { dataUrlToFile } from "./designModePrompt";
import { authenticatedFetch } from "./identity";
import { imagePreview, type ImageContentBlock, type MessageContentBlock } from "./blocks";

/** Web file-content path for an uploaded session attachment. */
export function sessionFileContentPath(sessionId: string, fileId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(fileId)}/content`;
}

/**
 * Re-encode `blob` as `image/png` if it isn't already.
 *
 * The clipboard write always announces `image/png`: Chromium rejects a blob
 * whose bytes don't match the MIME key it was written under, so a JPEG or
 * WebP upload has to be transcoded rather than copied as-is.
 */
async function toPngBlob(blob: Blob): Promise<Blob> {
  if (blob.type === "image/png") return blob;
  if (typeof createImageBitmap === "undefined" || typeof OffscreenCanvas === "undefined") {
    throw new Error("Image transcoding is not supported in this browser");
  }
  const bitmap = await createImageBitmap(blob);
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Could not get a 2D context to transcode the image");
  ctx.drawImage(bitmap, 0, 0);
  return canvas.convertToBlob({ type: "image/png" });
}

function uploadedLoader(sessionId: string, fileId: string): () => Promise<Blob> {
  const path = sessionFileContentPath(sessionId, fileId);
  return () =>
    authenticatedFetch(path)
      .then((res) => (res.ok ? res.blob() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then(toPngBlob);
}

/**
 * Loader for the message's first image, or `null` when that image can't be
 * loaded.
 *
 * Strictly the FIRST `input_image` block — never a later one. Copying a
 * "first image" that turns out to be the second one (because the real first
 * image was still uploading, or uploaded with no `sessionId` to fetch it, or
 * inline but undecodable) would hand the user a different image than the one
 * they meant to copy, with nothing telling them it was swapped. An
 * unresolvable first image falls back to a text-only copy instead.
 */
export function firstImageLoader(
  content: MessageContentBlock[],
  sessionId: string | null,
): (() => Promise<Blob>) | null {
  const block = content.find((c): c is ImageContentBlock => c.type === "input_image");
  if (!block) return null;
  const preview = imagePreview(block);
  if (preview.kind === "uploaded") {
    return sessionId ? uploadedLoader(sessionId, preview.fileId) : null;
  }
  if (preview.kind === "inline") {
    const file = dataUrlToFile(preview.src, preview.alt);
    return file ? () => toPngBlob(file) : null;
  }
  return null;
}
