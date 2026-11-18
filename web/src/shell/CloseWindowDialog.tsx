// Confirmation before Close (⌘W) closes the desktop window from the new-session
// page. Off macOS, closing the last window quits the app, so the copy says so.

import { useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface CloseWindowDialogProps {
  /** Set while a close awaits confirmation; `quits` when closing ends the app. */
  prompt: { quits: boolean } | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function CloseWindowDialog({ prompt, onConfirm, onCancel }: CloseWindowDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  // Keep the last prompt's copy on screen while the dialog fades out.
  const [shown, setShown] = useState(prompt);
  if (prompt !== null && prompt !== shown) setShown(prompt);
  const quits = shown?.quits === true;
  return (
    <Dialog open={prompt !== null} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent
        showCloseButton={false}
        onOpenAutoFocus={(event) => {
          // Enter confirms and Esc cancels, as in a native confirmation.
          event.preventDefault();
          confirmRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>{quits ? "Quit Omnigent?" : "Close this window?"}</DialogTitle>
          <DialogDescription>
            {quits
              ? "This is the only open window, so closing it quits Omnigent."
              : "Your sessions keep running."}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button ref={confirmRef} onClick={onConfirm}>
            {quits ? "Quit" : "Close Window"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
