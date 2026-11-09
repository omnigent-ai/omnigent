import {
  attachmentLabel,
  imagePreview,
  PENDING_FILE_PREFIX,
  type MessageContentBlock,
} from "@/lib/blocks";
import { showToast } from "@/components/ui/toast";
import { downloadSessionFile } from "@/lib/filesApi";

/**
 * Download a sent message's uploaded attachments so a composer can send them again.
 * Images an imported transcript carries inline as data URIs are decoded locally.
 *
 * @param sessionId - The session the message was sent in.
 * @param content - The message blocks; image and file blocks are recovered.
 * @returns The files that came back, and the names of those that didn't.
 */
export async function recoverMessageAttachments(
  sessionId: string | null,
  content: MessageContentBlock[],
): Promise<{ files: File[]; missing: string[] }> {
  const attachments = content.filter(
    (block) => block.type === "input_image" || block.type === "input_file",
  );
  const results = await Promise.allSettled(
    attachments.map(async (block) => {
      // Imported transcripts can hold any type in these fields.
      const filename = typeof block.filename === "string" ? block.filename : undefined;
      if (block.type === "input_image") {
        const preview = imagePreview(block);
        if (preview.kind === "inline") return decodeDataUri(preview.src, filename);
      }
      const fileId = typeof block.file_id === "string" ? block.file_id : "";
      if (sessionId !== null && fileId && !fileId.startsWith(PENDING_FILE_PREFIX)) {
        return downloadSessionFile(sessionId, fileId, filename ?? "attachment");
      }
      throw new Error("no uploaded file");
    }),
  );
  const files: File[] = [];
  const missing: string[] = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") files.push(result.value);
    else missing.push(withoutPendingPrefix(attachmentLabel(attachments[index]!)));
  });
  return { files, missing };
}

/** Tell the user which attachments didn't come back, if any. */
export function notifyMissingAttachments(missing: string[]): void {
  if (missing.length > 0) {
    showToast(`Couldn't copy ${missing.join(", ")}. Attach again before you send.`);
  }
}

/** Turn a `data:` URI into a File; throws when the URI is malformed. */
function decodeDataUri(uri: string, filename: string | undefined): File {
  const match = /^data:([^;,]*)((?:;[^;,]*)*),(.*)$/is.exec(uri);
  if (match === null) throw new Error("malformed data URI");
  const mimeType = match[1] || "application/octet-stream";
  const isBase64 = match[2]!.toLowerCase().split(";").includes("base64");
  const payload = match[3]!;
  // RFC 2397 payloads may be percent-encoded; non-base64 ones can carry raw bytes.
  const bytes = isBase64
    ? Uint8Array.from(atob(decodeURIComponent(payload)), (character) => character.charCodeAt(0))
    : percentDecodeBytes(payload);
  const extension = mimeType.split("/")[1]?.split("+")[0] ?? "bin";
  return new File([bytes], filename ?? `image.${extension}`, { type: mimeType });
}

/** Decode `%XX` escapes byte for byte, encoding other characters as UTF-8. */
function percentDecodeBytes(payload: string): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder();
  const bytes: number[] = [];
  for (const part of payload.split(/(%[0-9a-f]{2})/i)) {
    if (/^%[0-9a-f]{2}$/i.test(part)) bytes.push(Number.parseInt(part.slice(1), 16));
    else bytes.push(...encoder.encode(part));
  }
  return Uint8Array.from(bytes);
}

function withoutPendingPrefix(label: string): string {
  return label.startsWith(PENDING_FILE_PREFIX) ? label.slice(PENDING_FILE_PREFIX.length) : label;
}
