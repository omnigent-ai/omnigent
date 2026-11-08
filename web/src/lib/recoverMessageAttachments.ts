import { PENDING_FILE_PREFIX, type MessageContentBlock } from "@/lib/blocks";
import { downloadSessionFile } from "@/lib/filesApi";

/**
 * Download a sent message's uploaded attachments so a composer can send them again.
 *
 * @param sessionId - The session the message was sent in.
 * @param content - The message blocks; image and file blocks are downloaded.
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
    attachments.map((block) =>
      sessionId !== null && block.file_id && !block.file_id.startsWith(PENDING_FILE_PREFIX)
        ? downloadSessionFile(sessionId, block.file_id, block.filename ?? "attachment")
        : Promise.reject(new Error("no uploaded file")),
    ),
  );
  const files: File[] = [];
  const missing: string[] = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") files.push(result.value);
    else missing.push(attachments[index]!.filename ?? "an attachment");
  });
  return { files, missing };
}
