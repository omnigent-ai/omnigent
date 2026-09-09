// Resolves the first copyable image in a user message's content blocks into a
// loader the clipboard can pull bytes from, for "copy message" (see
// components/chat/chatBubbleParts.tsx and lib/clipboard.ts's copyTextWithImage).

import { dataUrlToFile } from "./designModePrompt";
import { authenticatedFetch } from "./identity";
import { imagePreview, type MessageContentBlock } from "./blocks";

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
 * Loader for the first copyable image in `content`, or `null` when none of
 * its `input_image` blocks can be loaded.
 *
 * Walks blocks in order and classifies each with `imagePreview`: an
 * `uploaded` block needs `sessionId` to build its fetch path, an `inline`
 * block decodes straight from its data URI, and `pending` / `unavailable`
 * blocks are skipped in favor of a later image.
 */
export function firstImageLoader(
  content: MessageContentBlock[],
  sessionId: string | null,
): (() => Promise<Blob>) | null {
  for (const block of content) {
    if (block.type !== "input_image") continue;
    const preview = imagePreview(block);
    if (preview.kind === "uploaded") {
      if (!sessionId) continue;
      return uploadedLoader(sessionId, preview.fileId);
    }
    if (preview.kind === "inline") {
      const file = dataUrlToFile(preview.src, preview.alt);
      if (!file) continue;
      return () => toPngBlob(file);
    }
  }
  return null;
}
