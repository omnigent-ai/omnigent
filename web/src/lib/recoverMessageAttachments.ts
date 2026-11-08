import {
  attachmentLabel,
  imagePreview,
  PENDING_FILE_PREFIX,
  type MessageContentBlock,
} from "@/lib/blocks";
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
    else missing.push(attachmentLabel(attachments[index]!));
  });
  return { files, missing };
}

/** Turn a `data:` URI into a File; throws when the URI is malformed. */
function decodeDataUri(uri: string, filename: string | undefined): File {
  const match = /^data:([^;,]*)((?:;[^;,]*)*),(.*)$/is.exec(uri);
  if (match === null) throw new Error("malformed data URI");
  const mimeType = match[1] || "application/octet-stream";
  const isBase64 = match[2]!.toLowerCase().split(";").includes("base64");
  const payload = match[3]!;
  const bytes = isBase64
    ? Uint8Array.from(atob(payload), (character) => character.charCodeAt(0))
    : new TextEncoder().encode(decodeURIComponent(payload));
  const extension = mimeType.split("/")[1]?.split("+")[0] ?? "bin";
  return new File([bytes], filename ?? `image.${extension}`, { type: mimeType });
}
