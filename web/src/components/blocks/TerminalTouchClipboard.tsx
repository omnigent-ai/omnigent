// Copy and paste for terminals on touch devices.
//
// xterm draws output on a canvas and keeps its input in a hidden textarea, so
// a phone or tablet has no long-press selection, Copy menu, or Paste target.
// This bar offers both: Copy opens the terminal text in a sheet that supports
// the platform's own selection gestures, and Paste reads the clipboard into
// the terminal (or, when clipboard reads are refused, offers a paste box).

import { ClipboardPasteIcon, CopyIcon } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type ClipboardEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { copyText } from "@/lib/clipboard";

export const TERMINAL_COPY_BLOCKED_MESSAGE = "Copying from this terminal is blocked.";

interface TerminalTouchClipboardBarProps {
  /** Background behind the bar, matching the terminal surface. */
  backgroundColor?: string;
  /** Whether Paste is offered at all (false for read-only terminals). */
  pasteAvailable: boolean;
  /** Whether Paste can send input now (the terminal is connected). */
  pasteEnabled: boolean;
  /** Reads the terminal's current text when the copy sheet opens. */
  readText: () => string;
  /** Whether the user's terminal clipboard choice blocks copying. */
  isCopyBlocked: () => boolean;
  /** Sends pasted text to the terminal as input. */
  onPaste: (text: string) => void;
}

export function TerminalTouchClipboardBar({
  backgroundColor,
  pasteAvailable,
  pasteEnabled,
  readText,
  isCopyBlocked,
  onPaste,
}: TerminalTouchClipboardBarProps) {
  const [sheetText, setSheetText] = useState<string | null>(null);
  const [pasteBoxOpen, setPasteBoxOpen] = useState(false);

  const openCopySheet = () => {
    if (isCopyBlocked()) {
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    setSheetText(readText());
  };

  const paste = async () => {
    // Read inside the tap so the platform can show its own paste confirmation.
    if (typeof navigator !== "undefined" && navigator.clipboard?.readText) {
      try {
        const text = await navigator.clipboard.readText();
        if (text) onPaste(text);
        else toast.info("The clipboard has no text to paste.");
        return;
      } catch {
        // Refused or unsupported reads fall back to a paste box below.
      }
    }
    setPasteBoxOpen(true);
  };

  return (
    <>
      <div
        role="toolbar"
        aria-label="Terminal clipboard"
        data-testid="terminal-touch-clipboard"
        className="flex shrink-0 items-center justify-end gap-2 px-3 pb-2"
        style={{ backgroundColor }}
      >
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={openCopySheet}
          componentId="terminal.touch_clipboard.copy"
        >
          <CopyIcon className="size-4" aria-hidden="true" />
          Copy
        </Button>
        {pasteAvailable && (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={!pasteEnabled}
            // Keep focus (and the on-screen keyboard) in the terminal.
            onPointerDown={(event) => event.preventDefault()}
            onClick={() => void paste()}
            componentId="terminal.touch_clipboard.paste"
          >
            <ClipboardPasteIcon className="size-4" aria-hidden="true" />
            Paste
          </Button>
        )}
      </div>
      <TerminalCopySheet
        text={sheetText}
        isCopyBlocked={isCopyBlocked}
        onClose={() => setSheetText(null)}
      />
      {pasteAvailable && (
        <TerminalPasteBox
          open={pasteBoxOpen}
          enabled={pasteEnabled}
          onSend={(text) => {
            setPasteBoxOpen(false);
            onPaste(text);
          }}
          onClose={() => setPasteBoxOpen(false)}
        />
      )}
    </>
  );
}

function TerminalCopySheet({
  text,
  isCopyBlocked,
  onClose,
}: {
  text: string | null;
  isCopyBlocked: () => boolean;
  onClose: () => void;
}) {
  const textRef = useRef<HTMLPreElement | null>(null);
  const [selection, setSelection] = useState("");
  const open = text !== null;

  // Track the selection inside the sheet. Tapping the Copy button can collapse
  // it on touch devices, so keep the last one until a tap inside the text clears it.
  useEffect(() => {
    if (!open) {
      setSelection("");
      return;
    }
    const onSelectionChange = () => {
      const current = document.getSelection();
      const node = textRef.current;
      if (!current || !node || current.rangeCount === 0) return;
      if (!node.contains(current.anchorNode)) return;
      setSelection(current.toString());
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
  }, [open]);

  // Start at the newest output, the part a user usually wants.
  const scrollToEnd = useCallback((node: HTMLPreElement | null) => {
    textRef.current = node;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  const copy = async () => {
    if (text === null) return;
    if (isCopyBlocked()) {
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    try {
      await copyText(selection || text);
    } catch {
      toast.error("Couldn't copy. Select the text and choose Copy from the menu.");
      return;
    }
    toast.success(selection ? "Copied selection" : "Copied terminal text");
    onClose();
  };

  const onNativeCopy = (event: ClipboardEvent<HTMLPreElement>) => {
    if (!isCopyBlocked()) return;
    event.preventDefault();
    toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="terminal-copy-sheet" className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Copy terminal text</DialogTitle>
          <DialogDescription>Press and hold to select text, or copy everything.</DialogDescription>
        </DialogHeader>
        {text ? (
          <pre
            ref={scrollToEnd}
            data-testid="terminal-copy-text"
            onCopy={onNativeCopy}
            className="max-h-[50vh] min-h-0 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap select-text"
          >
            {text}
          </pre>
        ) : (
          <p className="text-muted-foreground">This terminal has no text yet.</p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button
            type="button"
            disabled={!text}
            onClick={() => void copy()}
            componentId="terminal.touch_clipboard.copy_text"
          >
            {selection ? "Copy selection" : "Copy all"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TerminalPasteBox({
  open,
  enabled,
  onSend,
  onClose,
}: {
  open: boolean;
  enabled: boolean;
  onSend: (text: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState("");
  const descriptionId = useId();

  useEffect(() => {
    if (!open) setValue("");
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="terminal-paste-box">
        <DialogHeader>
          <DialogTitle>Paste into terminal</DialogTitle>
          <DialogDescription id={descriptionId}>
            Paste your text here, then send it to the terminal.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          autoFocus
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-label="Text to paste"
          aria-describedby={descriptionId}
          className="min-h-24 font-mono"
        />
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            disabled={!enabled || value === ""}
            onClick={() => onSend(value)}
            componentId="terminal.touch_clipboard.paste_box"
          >
            Send to terminal
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
