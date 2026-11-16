// Confirmation before Quit (⌘Q / Ctrl+Q) quits the desktop app, saying what
// stops. "Don't ask again" turns it off, as the menu's Warn Before Quitting does.

import { useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface QuitDialogProps {
  /** Set while a quit awaits confirmation; `detail` names what quitting stops. */
  prompt: { detail: string | null } | null;
  onConfirm: (dontAskAgain: boolean) => void;
  onCancel: () => void;
}

export function QuitDialog({ prompt, onConfirm, onCancel }: QuitDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const checkboxId = useId();
  const [dontAskAgain, setDontAskAgain] = useState(false);
  // Keep the last prompt's copy on screen while the dialog fades out, and start
  // each new prompt unchecked.
  const [shown, setShown] = useState(prompt);
  if (prompt !== null && prompt !== shown) {
    setShown(prompt);
    setDontAskAgain(false);
  }
  return (
    <Dialog open={prompt !== null} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent
        showCloseButton={false}
        onOpenAutoFocus={(event) => {
          // Enter quits and Esc cancels, as in a native confirmation.
          event.preventDefault();
          confirmRef.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>Quit Omnigent?</DialogTitle>
          <DialogDescription>{shown?.detail ?? "Your sessions keep running."}</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <Checkbox
            id={checkboxId}
            checked={dontAskAgain}
            onCheckedChange={(checked) => setDontAskAgain(checked === true)}
          />
          <label htmlFor={checkboxId} className="cursor-pointer text-ui">
            Don't ask again
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button ref={confirmRef} onClick={() => onConfirm(dontAskAgain)}>
            Quit
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
