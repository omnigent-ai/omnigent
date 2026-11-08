// Edit on a sent user message: load its text and attachments into the
// composer of the pane that shows it, leaving the transcript unchanged.
import { createContext, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
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

/** A recovered message waiting for the user to confirm it may replace their draft. */
interface PendingReplacement {
  target: string | null;
  draft: EditedDraft;
  missing: string[];
}

/**
 * Load a sent message into a composer, confirming first when a draft would be replaced.
 *
 * Attachments download before the draft check, so a draft typed during the
 * download still gets the confirmation. A newer Edit click, a change of
 * `target`, or unmounting drops a load still in flight.
 *
 * @param target - Identifies the draft the composer is showing (its conversation).
 * @param canAccept - Whether the composer can take a message now; read at click
 *   and again before anything is applied.
 * @param hasDraft - Whether the composer holds unsent input.
 * @param applyDraft - Replaces the composer's draft and focuses it.
 * @returns `editMessage` for {@link EditSentMessageContext}, and the confirmation
 *   dialog to render.
 */
export function useEditSentMessage({
  target,
  canAccept,
  hasDraft,
  applyDraft,
}: {
  target: string | null;
  canAccept: () => boolean;
  hasDraft: () => boolean;
  applyDraft: (draft: EditedDraft) => void;
}): { editMessage: (message: SentMessage) => void; confirmDialog: ReactNode } {
  const [pending, setPending] = useState<PendingReplacement | null>(null);
  const latestRef = useRef({ target, canAccept, hasDraft, applyDraft });
  latestRef.current = { target, canAccept, hasDraft, applyDraft };
  const loadSequenceRef = useRef(0);
  useEffect(
    () => () => {
      loadSequenceRef.current += 1;
    },
    [target],
  );

  const apply = useCallback((draft: EditedDraft, missing: string[]) => {
    if (missing.length > 0) {
      showToast(`Couldn't copy ${missing.join(", ")}. Attach again before you send.`);
    }
    latestRef.current.applyDraft(draft);
  }, []);

  const editMessage = useCallback(
    (message: SentMessage) => {
      const start = latestRef.current;
      if (!start.canAccept()) return;
      const sequence = ++loadSequenceRef.current;
      setPending(null);
      void recoverMessageAttachments(message.sessionId, message.content).then(
        ({ files, missing }) => {
          const latest = latestRef.current;
          const superseded = sequence !== loadSequenceRef.current || latest.target !== start.target;
          if (superseded || !latest.canAccept()) return;
          const draft = { text: message.text, files, mentions: message.mentions };
          if (latest.hasDraft()) setPending({ target: latest.target, draft, missing });
          else apply(draft, missing);
        },
      );
    },
    [apply],
  );

  const awaitingConfirmation = pending !== null && pending.target === target;
  const confirmDialog = (
    <ReplaceDraftDialog
      open={awaitingConfirmation}
      onConfirm={() => {
        setPending(null);
        if (awaitingConfirmation && latestRef.current.canAccept()) {
          apply(pending.draft, pending.missing);
        }
      }}
      onCancel={() => setPending(null)}
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
