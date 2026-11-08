import { useState } from "react";
import { CircleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import type { MessageContentBlock } from "@/lib/blocks";
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { recoverMessageAttachments } from "@/lib/recoverMessageAttachments";
import { useNavigate } from "@/lib/routing";

/**
 * Recovery actions for a startup input that was never delivered.
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
    const { files, missing } = await recoverMessageAttachments(sessionId, content);
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
        className="h-auto px-1 text-[11px] text-destructive underline underline-offset-2"
        disabled={starting}
        onClick={() => void startNewSession()}
      >
        Copy message to new session
      </Button>
    </div>
  );
}
