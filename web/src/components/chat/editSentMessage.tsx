// Edit on a sent user message: copy its text into the composer of the pane
// that shows it, leaving the transcript unchanged.
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

/** A sent user message, as its bubble shows it. */
export interface SentMessage {
  /** The text as sent, with its file and session mention markers. */
  text: string;
}

/**
 * The handler a user bubble's Edit action calls. `null` (no composer can take
 * the message, e.g. a read-only viewer) hides the action.
 */
export const EditSentMessageContext = createContext<((message: SentMessage) => void) | null>(null);

const COMPOSER_UNAVAILABLE = "The composer can't take a message right now.";

/**
 * Copy a sent message's text into a composer, asking first when it holds a draft.
 * `focusComposer` refocuses the composer after the dialog's focus trap releases.
 */
export function useEditSentMessage({
  target,
  canAccept,
  hasDraft,
  loadText,
  focusComposer,
}: {
  target: string | null;
  canAccept: () => boolean;
  hasDraft: () => boolean;
  /** Replace the composer's draft, attachments included, with ``text``. */
  loadText: (text: string) => void;
  focusComposer: () => void;
}): { editMessage: (message: SentMessage) => void; confirmDialog: ReactNode } {
  const [pending, setPending] = useState<{ target: string | null; message: SentMessage } | null>(
    null,
  );
  const latestRef = useRef({ target, canAccept, hasDraft, loadText, focusComposer });
  latestRef.current = { target, canAccept, hasDraft, loadText, focusComposer };
  // Set once a message loads, so the closing dialog hands focus to the composer.
  const loadedRef = useRef(false);
  useEffect(() => () => setPending(null), [target]);

  const load = useCallback((message: SentMessage) => {
    const latest = latestRef.current;
    if (!latest.canAccept()) {
      showToast(COMPOSER_UNAVAILABLE);
      return;
    }
    loadedRef.current = true;
    latest.loadText(message.text);
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
  return { editMessage, confirmDialog };
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
