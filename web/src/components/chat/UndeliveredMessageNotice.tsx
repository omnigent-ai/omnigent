import { useState } from "react";
import { CircleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { PENDING_FILE_PREFIX, type MessageContentBlock } from "@/lib/blocks";
import { downloadSessionFile } from "@/lib/filesApi";
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { useNavigate } from "@/lib/routing";

/**
 * Download a message's uploaded attachments so a new session can send them again.
 *
 * @returns The files that came back, and the names of those that didn't.
 */
async function recoverAttachments(
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

/**
 * The status line under a startup message the server never delivered, with a
 * way to start a new session from it. The failed session has no host to resend
 * on, so the message (text and attachments) moves to the new-session composer.
 *
 * @param sessionId - The session the message was saved in.
 * @param text - The message text, as the bubble's Copy action copies it.
 * @param content - The message blocks; uploaded attachments are downloaded.
 */
export function UndeliveredMessageNotice({
  sessionId,
  text,
  content,
}: {
  sessionId: string | null;
  text: string;
  content: MessageContentBlock[];
}) {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);

  async function startNewSession() {
    setStarting(true);
    const { files, missing } = await recoverAttachments(sessionId, content);
    if (missing.length > 0) {
      showToast(`Couldn't copy ${missing.join(", ")}. Attach again before you send.`);
    }
    navigate(newSessionLocation(text, files));
  }

  return (
    <div
      className="flex items-center gap-1 pt-1 text-[11px] leading-4 text-destructive"
      data-testid="message-undelivered"
    >
      <CircleAlertIcon size={12} aria-hidden />
      Not delivered
      <Button
        variant="link"
        size="xs"
        className="h-auto px-1 text-[11px]"
        disabled={starting}
        onClick={() => void startNewSession()}
      >
        Start a new session with this message
      </Button>
    </div>
  );
}
