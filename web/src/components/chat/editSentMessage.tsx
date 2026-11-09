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
import {
  notifyMissingAttachments,
  recoverMessageAttachments,
} from "@/lib/recoverMessageAttachments";

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

const COMPOSER_UNAVAILABLE = "The composer can't take a message right now.";

type RecoveredAttachments = Awaited<ReturnType<typeof recoverMessageAttachments>>;

/** A message waiting for the user to confirm it may replace their draft. */
interface PendingReplacement {
  target: string | null;
  message: SentMessage;
  /** Already downloaded when the draft was typed during the download. */
  recovered: RecoveredAttachments | null;
  /** Confirmed, and downloading the attachments. */
  replacing: boolean;
}

/**
 * Load a sent message into a composer, confirming before a draft is replaced
 * and dropping loads a newer Edit, Keep draft, or a change of `target` supersedes.
 * `focusComposer` refocuses the composer after the dialog's focus trap releases.
 */
export function useEditSentMessage({
  target,
  canAccept,
  hasDraft,
  applyDraft,
  focusComposer,
}: {
  target: string | null;
  canAccept: () => boolean;
  hasDraft: () => boolean;
  applyDraft: (draft: EditedDraft) => void;
  focusComposer: () => void;
}): { editMessage: (message: SentMessage) => void; confirmDialog: ReactNode } {
  const [pending, setPending] = useState<PendingReplacement | null>(null);
  const latestRef = useRef({ target, canAccept, hasDraft, applyDraft, focusComposer });
  latestRef.current = { target, canAccept, hasDraft, applyDraft, focusComposer };
  const loadSequenceRef = useRef(0);
  // Set once a draft is applied, so the closing dialog hands focus to the composer.
  const appliedRef = useRef(false);
  useEffect(
    () => () => {
      loadSequenceRef.current += 1;
      setPending(null);
    },
    [target],
  );

  const apply = useCallback((message: SentMessage, { files, missing }: RecoveredAttachments) => {
    notifyMissingAttachments(missing);
    appliedRef.current = true;
    latestRef.current.applyDraft({ text: message.text, files, mentions: message.mentions });
  }, []);

  const load = useCallback(
    (message: SentMessage, confirmed: boolean) => {
      const sequence = ++loadSequenceRef.current;
      const startTarget = latestRef.current.target;
      recoverMessageAttachments(message.sessionId, message.content)
        .then((recovered) => {
          if (sequence !== loadSequenceRef.current) return;
          const latest = latestRef.current;
          if (latest.target !== startTarget) {
            setPending(null);
          } else if (!latest.canAccept()) {
            setPending(null);
            showToast(COMPOSER_UNAVAILABLE);
          } else if (!confirmed && latest.hasDraft()) {
            setPending({ target: latest.target, message, recovered, replacing: false });
          } else {
            setPending(null);
            apply(message, recovered);
          }
        })
        .catch(() => {
          if (sequence !== loadSequenceRef.current) return;
          setPending(null);
          showToast("Couldn't load that message into the composer.");
        });
    },
    [apply],
  );

  const editMessage = useCallback(
    (message: SentMessage) => {
      const latest = latestRef.current;
      appliedRef.current = false;
      if (!latest.canAccept()) {
        showToast(COMPOSER_UNAVAILABLE);
        return;
      }
      if (latest.hasDraft()) {
        loadSequenceRef.current += 1;
        setPending({ target: latest.target, message, recovered: null, replacing: false });
      } else {
        setPending(null);
        load(message, false);
      }
    },
    [load],
  );

  const current = pending !== null && pending.target === target ? pending : null;
  const confirmDialog = (
    <ReplaceDraftDialog
      open={current !== null}
      replacing={current?.replacing ?? false}
      onCloseAutoFocus={(event) => {
        // The open dialog traps the composer's own focus call.
        if (!appliedRef.current) return;
        event.preventDefault();
        latestRef.current.focusComposer();
      }}
      onConfirm={() => {
        if (current === null || current.replacing) return;
        if (!latestRef.current.canAccept()) {
          setPending(null);
          showToast(COMPOSER_UNAVAILABLE);
        } else if (current.recovered !== null) {
          setPending(null);
          apply(current.message, current.recovered);
        } else {
          setPending({ ...current, replacing: true });
          load(current.message, true);
        }
      }}
      onCancel={() => {
        loadSequenceRef.current += 1;
        setPending(null);
      }}
    />
  );
  return { editMessage, confirmDialog };
}

function ReplaceDraftDialog({
  open,
  replacing,
  onCloseAutoFocus,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  replacing: boolean;
  onCloseAutoFocus: (event: Event) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
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
          <Button onClick={onConfirm} disabled={replacing}>
            {replacing ? "Replacing…" : "Replace draft"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
