import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { CheckIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { isImeCompositionKeyEvent } from "@/lib/ime";

interface SidebarEditRowProps {
  initialTitle: string;
  onCommit: (title: string) => void;
  onCancel: () => void;
  label?: string;
}

/** Shared inline rename: Enter or blur saves; Escape cancels. */
export function SidebarEditRow({
  initialTitle,
  onCommit,
  onCancel,
  label = "Rename session",
}: SidebarEditRowProps) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  // Cancelling must not commit on the subsequent blur.
  const cancelledRef = useRef(false);
  // Enter confirming an IME candidate must not save the rename.
  const isComposingRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (isImeCompositionKeyEvent(e, isComposingRef.current)) return;
    if (e.key === "Enter") {
      e.preventDefault();
      onCommit(value);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      cancelledRef.current = true;
      onCancel();
    }
  }

  function handleBlur() {
    if (cancelledRef.current) return;
    onCommit(value);
  }

  return (
    // Match the interactive row's responsive box metrics so entering edit mode
    // doesn't shift the list. pl-1 + the input's px-1 align with row titles.
    <div className="sidebar-compact-text flex h-8 items-center gap-1 rounded-[var(--radius-otto-sm)] bg-muted pr-1 pl-1 md:h-7">
      <input
        ref={inputRef}
        type="text"
        aria-label={label}
        maxLength={USER_SESSION_TITLE_MAX_CHARS}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onCompositionStart={() => {
          isComposingRef.current = true;
        }}
        onCompositionEnd={() => {
          isComposingRef.current = false;
        }}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        data-testid="rename-conversation-input"
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-0.5 outline-none md:select-text"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Save rename"
        onMouseDown={(e) => {
          // Prevent the input's blur from firing before the commit.
          e.preventDefault();
        }}
        onClick={() => onCommit(value)}
      >
        <CheckIcon className="size-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Cancel rename"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          cancelledRef.current = true;
          onCancel();
        }}
      >
        <XIcon className="size-3.5" />
      </Button>
    </div>
  );
}
