// Images and videos attached to a pull request description or comment: which files qualify, the
// provider's size limits, one upload driver, and delivery of results to the draft that started them.

import {
  type PullRequestAttachment,
  uploadPullRequestAttachment,
} from "@/newPullRequestPage/hooks/usePullRequests";

/** The media types the provider accepts as attachments. */
export const ATTACHMENT_ACCEPT =
  "image/png,image/jpeg,image/gif,image/webp,video/mp4,video/quicktime,video/webm";

export function isAttachableMedia(file: File): boolean {
  return file.type.startsWith("image/") || file.type.startsWith("video/");
}

// The server's per-type limits, checked here so a too-large file isn't sent first.
const MAX_IMAGE_ATTACHMENT_MB = 10;
const MAX_VIDEO_ATTACHMENT_MB = 70;

/** Why `file` is too large to attach, or `null` when it fits. */
export function attachmentSizeError(file: File): string | null {
  const video = file.type.startsWith("video/");
  const limitMb = video ? MAX_VIDEO_ATTACHMENT_MB : MAX_IMAGE_ATTACHMENT_MB;
  if (file.size <= limitMb * 1024 * 1024) return null;
  return `Couldn’t attach ${file.name || "the file"}: ${video ? "a video" : "an image"} can be at most ${limitMb} MB.`;
}

/** The markdown that embeds an uploaded attachment; a bare video URL renders as a player. */
export function attachmentMarkdown(attachment: PullRequestAttachment, fileName: string): string {
  if (attachment.content_type.startsWith("video/")) return attachment.url;
  const alt = fileName.replace(/\.[^.]+$/, "").replace(/[[\]]/g, "") || "image";
  return `![${alt}](${attachment.url})`;
}

let lastUploadId = 0;

/** The next upload's id, unique on the page, for the placeholder that stands in for it. */
export function nextUploadId(): string {
  lastUploadId += 1;
  return `upload-${lastUploadId}`;
}

/** The Markdown placeholder for an upload in progress. */
export function uploadPlaceholderMarkdown(id: string, fileName: string): string {
  // Brackets would end the alt text early, so the placeholder couldn't be found again.
  const name = fileName.replace(/[[\]\n]/g, "") || "file";
  return `![Uploading ${name}…](#${id})`;
}

/** Where an editor shows the progress of each upload it started. */
export interface AttachmentUploadTarget {
  /** A file was accepted and is uploading; show a placeholder for `id`. */
  start: (id: string, fileName: string) => void;
  /** The upload finished: replace `id`'s placeholder with `markdown`. */
  finish: (id: string, markdown: string) => void;
  /** The upload failed: remove `id`'s placeholder and show `error`. */
  fail: (id: string, error: string) => void;
  /** A file was refused before uploading. */
  refuse: (error: string) => void;
}

export interface AttachmentUploadContext {
  conversationId: string;
  prUrl: string;
  /** Keeps a local copy of an uploaded image until the provider serves it. */
  rememberUploadedImage: (url: string, file: File) => Promise<void>;
}

/** Upload each attachable file, reporting to `target`; refused files are reported first. */
export function uploadAttachments(
  files: readonly File[],
  { conversationId, prUrl, rememberUploadedImage }: AttachmentUploadContext,
  target: AttachmentUploadTarget,
): void {
  const attachable = files.filter(isAttachableMedia);
  if (attachable.length < files.length) target.refuse("Only images and videos can be attached.");
  const sizeErrors = attachable.map(attachmentSizeError);
  const tooLarge = sizeErrors.find((error) => error !== null);
  if (tooLarge) target.refuse(tooLarge);
  for (const file of attachable.filter((_file, index) => sizeErrors[index] === null)) {
    const id = nextUploadId();
    target.start(id, file.name);
    uploadPullRequestAttachment(conversationId, prUrl, file).then(
      async (attachment) => {
        // The local copy is ready before the body links the upload.
        await rememberUploadedImage(attachment.url, file);
        target.finish(id, attachmentMarkdown(attachment, file.name));
      },
      (error: unknown) =>
        target.fail(
          id,
          `Couldn’t attach ${file.name || "the file"}: ${
            error instanceof Error ? error.message : String(error)
          }`,
        ),
    );
  }
}

/** Reset upload ids between tests. */
export function resetUploadIdsForTests(): void {
  lastUploadId = 0;
}

// ── Upload delivery ─────────────────────────────────────────────────────────
// An upload can finish after its composer closed, or after the draft reopened in another one.
// Mounted composers register here; a finished upload goes to whichever still shows its
// placeholder, else the caller applies it to the stored draft.

/** The Markdown placeholder for `uploadId`, with the line break a failed upload removes too. */
export function uploadPlaceholderPattern(uploadId: string, withNewline = false): RegExp {
  // Placeholder names carry no brackets, so a match can't run on into a neighbouring image.
  return new RegExp(`!\\[[^\\]\\n]*\\]\\(#${uploadId}\\)${withNewline ? "\\r?\\n?" : ""}`);
}

/** `text` with `uploadId`'s placeholder replaced by its result, or removed after a failure. */
export function applyUploadToText(text: string, uploadId: string, markdown: string | null): string {
  return text.replace(uploadPlaceholderPattern(uploadId, markdown === null), markdown ?? "");
}

// Each settled upload's result (its Markdown, or null after a failure), so a placeholder that
// Undo brings back is resolved instead of waiting for an upload that already ended.
const MAX_SETTLED_UPLOADS = 200;
const settledUploads = new Map<string, string | null>();

/** Remember how `uploadId` settled; call before applying its result anywhere. */
export function recordSettledUpload(uploadId: string, markdown: string | null): void {
  settledUploads.set(uploadId, markdown);
  if (settledUploads.size > MAX_SETTLED_UPLOADS) {
    settledUploads.delete(settledUploads.keys().next().value!);
  }
}

const ANY_UPLOAD_PLACEHOLDER = /!\[[^\]\n]*\]\(#(upload-\d+)\)/g;

/** `text` with every placeholder whose upload already settled replaced by the result. */
export function resolveSettledUploads(text: string): string {
  if (settledUploads.size === 0) return text;
  let resolved = text;
  for (const [, uploadId] of text.matchAll(ANY_UPLOAD_PLACEHOLDER)) {
    const result = settledUploads.get(uploadId);
    if (result !== undefined) resolved = applyUploadToText(resolved, uploadId, result);
  }
  return resolved;
}

/** Something showing upload placeholders: applies a finished upload if it holds `uploadId`. */
export type UploadHost = (uploadId: string, markdown: string | null) => boolean;

const uploadHosts = new Set<UploadHost>();

/** Register a mounted composer's upload host; returns the unregister. */
export function registerUploadHost(host: UploadHost): () => void {
  uploadHosts.add(host);
  return () => uploadHosts.delete(host);
}

/** Hand a finished (`markdown`) or failed (null) upload to the composer showing it, if any. */
export function deliverUpload(uploadId: string, markdown: string | null): boolean {
  for (const host of uploadHosts) if (host(uploadId, markdown)) return true;
  return false;
}

/** Forget settled uploads between tests. */
export function resetSettledUploadsForTests(): void {
  settledUploads.clear();
}
