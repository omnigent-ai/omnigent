// Edit on a sent user message: load its text and attachments into the
// composer of the pane that shows it, leaving the transcript unchanged.
import { createContext, useCallback, useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import type { MessageContentBlock } from "@/lib/blocks";
import type { MentionItem } from "@/lib/composerMentions";
import { recoverMessageAttachments } from "@/lib/recoverMessageAttachments";

/** A sent user message, as its bubble shows it. */
export interface SentMessage {
  /** The session the message was sent in, for downloading its attachments. */
  sessionId: string | null;
  /** The text the bubble's Copy action copies. */
  text: string;
  /** The message blocks; uploaded images and files are downloaded. */
  content: MessageContentBlock[];
  /** Workspace files and folders the message "@"-mentioned. */
  mentions: MentionItem[];
}

/** What the composer receives in place of its draft. */
export interface EditedDraft {
  text: string;
  files: File[];
  mentions: MentionItem[];
}

/**
 * The handler a user bubble's Edit action calls. `null` (no composer can take
 * the message, e.g. a read-only viewer) hides the action.
 */
export const EditSentMessageContext = createContext<((message: SentMessage) => void) | null>(null);

/**
 * Load a sent message into a composer, confirming first when a draft would be replaced.
 *
 * @param hasDraft - Reads at click time whether the composer holds unsent input.
 * @param applyDraft - Replaces the composer's draft and focuses it.
 * @returns `editMessage` for {@link EditSentMessageContext}, and the confirmation
 *   dialog to render.
 */
export function useEditSentMessage({
  hasDraft,
  applyDraft,
}: {
  hasDraft: () => boolean;
  applyDraft: (draft: EditedDraft) => void;
}): { editMessage: (message: SentMessage) => void; confirmDialog: ReactNode } {
  const [awaitingConfirmation, setAwaitingConfirmation] = useState<SentMessage | null>(null);
  const callbacksRef = useRef({ hasDraft, applyDraft });
  callbacksRef.current = { hasDraft, applyDraft };

  const load = useCallback(async (message: SentMessage) => {
    const { files, missing } = await recoverMessageAttachments(message.sessionId, message.content);
    if (missing.length > 0) {
      showToast(`Couldn't copy ${missing.join(", ")}. Attach again before you send.`);
    }
    callbacksRef.current.applyDraft({ text: message.text, files, mentions: message.mentions });
  }, []);

  const editMessage = useCallback(
    (message: SentMessage) => {
      if (callbacksRef.current.hasDraft()) setAwaitingConfirmation(message);
      else void load(message);
    },
    [load],
  );

  const confirmDialog = (
    <ReplaceDraftDialog
      open={awaitingConfirmation !== null}
      onConfirm={() => {
        const message = awaitingConfirmation;
        setAwaitingConfirmation(null);
        if (message !== null) void load(message);
      }}
      onCancel={() => setAwaitingConfirmation(null)}
    />
  );
  return { editMessage, confirmDialog };
}

function ReplaceDraftDialog({
  open,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Replace your draft?</DialogTitle>
          <DialogDescription>
            The composer already has an unsent message. Editing this message replaces that text and
            its attachments.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Keep draft
          </Button>
          <Button onClick={onConfirm}>Replace draft</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
