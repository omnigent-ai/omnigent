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
import {
  notifyMissingAttachments,
  recoverMessageAttachments,
} from "@/lib/recoverMessageAttachments";

/** A sent user message, as its bubble shows it. */
export interface SentMessage {
  /** The session the message was sent in, for downloading its attachments. */
  sessionId: string | null;
  /** The text as sent, with its file and session mention markers. */
  text: string;
  /** The message blocks; uploaded images and files are downloaded. */
  content: MessageContentBlock[];
}

/**
 * The handler a user bubble's Edit action calls. `null` (no composer can take
 * the message, e.g. a read-only viewer) hides the action.
 */
export const EditSentMessageContext = createContext<((message: SentMessage) => void) | null>(null);

const COMPOSER_UNAVAILABLE = "The composer can't take a message right now.";
const ATTACHMENTS_NOT_SENT =
  "The edited message's attachments were still loading, so they weren't sent.";

/**
 * Load a sent message into a composer, asking first when it holds a draft.
 * The text lands at once; recovered attachments are added when they arrive,
 * unless a newer Edit, a change of `target`, or a send came first. The composer
 * reports its sends through `draftSent`, which drops attachments still loading.
 * `focusComposer` refocuses the composer after the dialog's focus trap releases.
 */
export function useEditSentMessage({
  target,
  canAccept,
  hasDraft,
  loadText,
  addFiles,
  focusComposer,
}: {
  target: string | null;
  canAccept: () => boolean;
  hasDraft: () => boolean;
  /** Replace the composer's draft, attachments included, with ``text``. */
  loadText: (text: string) => void;
  addFiles: (files: File[]) => void;
  focusComposer: () => void;
}): {
  editMessage: (message: SentMessage) => void;
  draftSent: () => void;
  confirmDialog: ReactNode;
} {
  const [pending, setPending] = useState<{ target: string | null; message: SentMessage } | null>(
    null,
  );
  const latestRef = useRef({ target, canAccept, hasDraft, loadText, addFiles, focusComposer });
  latestRef.current = { target, canAccept, hasDraft, loadText, addFiles, focusComposer };
  const loadRef = useRef(0);
  // Whether the latest load's attachments are still downloading.
  const loadingRef = useRef(false);
  // Set once a message loads, so the closing dialog hands focus to the composer.
  const loadedRef = useRef(false);
  useEffect(
    () => () => {
      loadRef.current += 1;
      loadingRef.current = false;
      setPending(null);
    },
    [target],
  );

  const load = useCallback((message: SentMessage) => {
    const latest = latestRef.current;
    if (!latest.canAccept()) {
      showToast(COMPOSER_UNAVAILABLE);
      return;
    }
    const load = ++loadRef.current;
    loadedRef.current = true;
    loadingRef.current = true;
    latest.loadText(message.text);
    recoverMessageAttachments(message.sessionId, message.content)
      .then(({ files, missing }) => {
        const current = latestRef.current;
        if (load !== loadRef.current || current.target !== latest.target) return;
        loadingRef.current = false;
        notifyMissingAttachments(missing);
        if (files.length > 0) current.addFiles(files);
      })
      .catch(() => {
        if (load !== loadRef.current) return;
        loadingRef.current = false;
        showToast("Couldn't load that message's attachments.");
      });
  }, []);

  const editMessage = useCallback(
    (message: SentMessage) => {
      const latest = latestRef.current;
      loadedRef.current = false;
      if (!latest.canAccept()) {
        showToast(COMPOSER_UNAVAILABLE);
      } else if (latest.hasDraft()) {
        setPending({ target: latest.target, message });
      } else {
        load(message);
      }
    },
    [load],
  );

  const draftSent = useCallback(() => {
    if (!loadingRef.current) return;
    loadRef.current += 1;
    loadingRef.current = false;
    showToast(ATTACHMENTS_NOT_SENT);
  }, []);

  const current = pending !== null && pending.target === target ? pending : null;
  const confirmDialog = (
    <ReplaceDraftDialog
      open={current !== null}
      onCloseAutoFocus={(event) => {
        // The open dialog traps the composer's own focus call.
        if (!loadedRef.current) return;
        event.preventDefault();
        latestRef.current.focusComposer();
      }}
      onConfirm={() => {
        if (current === null) return;
        setPending(null);
        load(current.message);
      }}
      onCancel={() => setPending(null)}
    />
  );
  return { editMessage, draftSent, confirmDialog };
}

function ReplaceDraftDialog({
  open,
  onCloseAutoFocus,
  onConfirm,
  onCancel,
}: {
  open: boolean;
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
          <Button onClick={onConfirm}>Replace draft</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
