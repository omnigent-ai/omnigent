// Touch copy and paste for canvas-rendered terminals, which offer touch
// browsers no native selection or paste target.

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
import {
  isTerminalClipboardWritePending,
  writeExplicitTerminalClipboardText,
} from "@/lib/terminalClipboardWriter";

export const TERMINAL_COPY_BLOCKED_MESSAGE = "Copying from this terminal is blocked.";
const PASTE_NOT_CONNECTED_MESSAGE = "The terminal isn't connected, so nothing was pasted.";

/**
 * What happened to pasted text: sent, refused because the terminal can't take
 * input, or held back so the user can review multi-line text first.
 */
export type TerminalPasteResult = "pasted" | "unavailable" | "review";

interface PasteBoxState {
  text: string;
  review: boolean;
}

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
  /** Sends pasted text to the terminal; `reviewed` skips the multi-line review. */
  onPaste: (text: string, reviewed: boolean) => TerminalPasteResult;
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
  const [pasteBox, setPasteBox] = useState<PasteBoxState | null>(null);
  // A clipboard read can outlast the iOS paste prompt, so its result uses the latest props.
  const latestRef = useRef({ pasteAvailable, onPaste, mounted: true });
  latestRef.current = { ...latestRef.current, pasteAvailable, onPaste };
  useEffect(() => {
    latestRef.current.mounted = true;
    return () => {
      latestRef.current.mounted = false;
    };
  }, []);

  // A paste box left open must not reappear when a read-only terminal becomes writable.
  useEffect(() => {
    if (!pasteAvailable) setPasteBox(null);
  }, [pasteAvailable]);

  const openCopySheet = () => {
    if (isCopyBlocked()) {
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    setSheetText(readText());
  };

  const paste = async () => {
    // Read inside the tap so the platform can show its own paste confirmation.
    let text: string | null;
    try {
      text = await navigator.clipboard.readText();
    } catch {
      text = null;
    }
    const latest = latestRef.current;
    if (!latest.mounted || !latest.pasteAvailable) return;
    // Refused or unsupported reads fall back to a paste box.
    if (text === null) setPasteBox({ text: "", review: false });
    else if (!text) toast.info("The clipboard has no text to paste.");
    else showPasteResult(latest.onPaste(text, false), text);
  };

  const showPasteResult = (result: TerminalPasteResult, text: string) => {
    if (result === "pasted") setPasteBox(null);
    else if (result === "review") setPasteBox({ text, review: true });
    else toast.error(PASTE_NOT_CONNECTED_MESSAGE);
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
          state={pasteBox}
          enabled={pasteEnabled}
          onSend={(text) => showPasteResult(onPaste(text, true), text)}
          onClose={() => setPasteBox(null)}
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
      if (node.contains(current.anchorNode) && node.contains(current.focusNode)) {
        setSelection(current.toString());
      } else if (!current.isCollapsed) {
        // A selection dragged out of the text no longer matches the last one.
        setSelection("");
      }
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
    if (!(await writeExplicitTerminalClipboardText(selection || text))) {
      toast.error("Couldn't copy. Select the text and choose Copy from the menu.");
      return;
    }
    toast.success(selection ? "Copied selection" : "Copied terminal text");
    onClose();
  };

  const onNativeCopy = (event: ClipboardEvent<HTMLPreElement>) => {
    if (isCopyBlocked()) {
      event.preventDefault();
      toast.info(TERMINAL_COPY_BLOCKED_MESSAGE, { id: "terminal-clipboard-blocked" });
      return;
    }
    // The browser copies the selection now. A program copy still in flight could
    // land after it, so write the selection again once that copy finishes.
    const selected = document.getSelection()?.toString();
    if (selected && isTerminalClipboardWritePending()) {
      void writeExplicitTerminalClipboardText(selected);
    }
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
  state,
  enabled,
  onSend,
  onClose,
}: {
  state: PasteBoxState | null;
  enabled: boolean;
  onSend: (text: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState("");
  const descriptionId = useId();
  const open = state !== null;
  const initialText = state?.text ?? "";

  useEffect(() => {
    setValue(open ? initialText : "");
  }, [open, initialText]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="terminal-paste-box">
        <DialogHeader>
          <DialogTitle>Paste into terminal</DialogTitle>
          <DialogDescription id={descriptionId}>
            {state?.review
              ? "This text has more than one line, and each line may run as a command. Check it, then send it."
              : "Paste your text here, then send it to the terminal."}
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
