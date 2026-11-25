import { useState } from "react";
import { CircleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { PENDING_FILE_PREFIX, type MessageContentBlock } from "@/lib/blocks";
import { downloadSessionFile } from "@/lib/filesApi";
import { dismissFailedInput, retryFailedInput } from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
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
 * Recovery actions for a startup input that was never delivered.
 *
 * @param sessionId - The session the message was saved in.
 * @param pendingId - The undelivered message's id, for Send again and Dismiss.
 * @param text - The message text, as the bubble's Copy action copies it.
 * @param content - The message blocks; uploaded attachments are downloaded.
 */
export function UndeliveredMessageNotice({
  sessionId,
  pendingId,
  text,
  content,
}: {
  sessionId: string | null;
  pendingId?: string | null;
  text: string;
  content: MessageContentBlock[];
}) {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const [busy, setBusy] = useState(false);

  async function settle(action: typeof retryFailedInput, failure: string) {
    if (sessionId === null || !pendingId) return;
    setBusy(true);
    try {
      // Unchanged means another client already acted on it: drop it here too.
      await action(sessionId, pendingId);
      useChatStore.getState().forgetFailedInput(sessionId, pendingId);
    } catch {
      showToast(failure);
      setBusy(false);
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
      {sessionId !== null && pendingId ? (
        <>
          <Button
            variant="link"
            size="xs"
            className="h-auto px-1 text-[11px] text-destructive underline underline-offset-2"
            disabled={busy}
            onClick={() => void settle(retryFailedInput, "Couldn't send it again. Try again.")}
          >
            Send again
          </Button>
          <Button
            variant="link"
            size="xs"
            className="h-auto px-1 text-[11px] text-destructive underline underline-offset-2"
            disabled={busy}
            onClick={() => void settle(dismissFailedInput, "Couldn't dismiss it. Try again.")}
          >
            Dismiss
          </Button>
        </>
      ) : null}
      <Button
        variant="link"
        size="xs"
        className="h-auto px-1 text-[11px] text-destructive underline underline-offset-2"
        disabled={starting}
        onClick={() => void startNewSession()}
      >
        Copy message to new session
      </Button>
    </div>
  );
}
