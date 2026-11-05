import { useState } from "react";
import { CircleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { PENDING_FILE_PREFIX, type MessageContentBlock } from "@/lib/blocks";
import { downloadSessionFile } from "@/lib/filesApi";
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { useNavigate } from "@/lib/routing";
import { retryFailedStartup } from "@/store/chatStore";

const LINK_CLASS = "h-auto px-1 text-[11px] text-destructive underline underline-offset-2";

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
 * The status line under a startup message the server never delivered.
 * "Relaunch sandbox" asks the server to deliver it in this session, starting a
 * new sandbox when the first one never came up; "Copy message to new session"
 * moves the message (text and attachments) to the new-session composer instead.
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
  const [retrying, setRetrying] = useState(false);

  async function tryAgain() {
    if (sessionId === null) return;
    setRetrying(true);
    try {
      await retryFailedStartup(sessionId);
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "Couldn't relaunch the sandbox. Try again later.",
      );
    } finally {
      setRetrying(false);
    }
  }

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
        className={LINK_CLASS}
        disabled={retrying || starting || sessionId === null}
        onClick={() => void tryAgain()}
      >
        Relaunch sandbox
      </Button>
      <span aria-hidden>·</span>
      <Button
        variant="link"
        size="xs"
        className={LINK_CLASS}
        disabled={retrying || starting}
        onClick={() => void startNewSession()}
      >
        Copy message to new session
      </Button>
    </div>
  );
}
