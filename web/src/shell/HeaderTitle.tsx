import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useRenameConversation } from "@/hooks/useConversations";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";

/**
 * The breadcrumb title as a click-to-rename control (desktop).
 *
 * Clicking the title swaps it for an inline input so the session can be renamed
 * straight from the header — no trip to the kebab menu. Enter commits, Escape
 * cancels, blur commits (matching the sidebar's inline rename). Desktop only
 * and only for owner-managed top-level sessions; mobile keeps the Rename item
 * in the header session menu, since the native shells hide the breadcrumb.
 */
export function HeaderTitle({ conversationId, title }: { conversationId: string; title: string }) {
  const rename = useRenameConversation();
  const [editing, setEditing] = useState(false);

  if (!editing) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            data-testid="header-title"
            onClick={() => setEditing(true)}
            className="min-w-0 cursor-pointer truncate rounded text-left text-foreground hover:underline focus-visible:underline focus-visible:outline-none"
          >
            {title}
          </button>
        </TooltipTrigger>
        {/* Bottom placement: the header sits at top-0, so a top-side tooltip
            would clip above the viewport edge. */}
        <TooltipContent side="bottom">Rename</TooltipContent>
      </Tooltip>
    );
  }

  return (
    <HeaderTitleInput
      initialTitle={title}
      pending={rename.isPending}
      onCommit={(next) => {
        const trimmed = next.trim();
        if (trimmed && trimmed !== title) {
          rename.mutate({ id: conversationId, title: trimmed });
        }
        setEditing(false);
      }}
      onCancel={() => setEditing(false)}
    />
  );
}

/**
 * Inline rename input. Auto-focuses and selects the whole title on mount so a
 * user can type to replace. Enter commits, Escape cancels, blur commits — with
 * an IME-composition guard so the Enter that confirms a candidate (e.g.
 * Japanese conversion) doesn't commit the rename, and a cancel flag so the
 * unmount blur doesn't re-commit after Escape.
 */
function HeaderTitleInput({
  initialTitle,
  pending,
  onCommit,
  onCancel,
}: {
  initialTitle: string;
  pending: boolean;
  onCommit: (title: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelledRef = useRef(false);
  const isComposingRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (isImeCompositionKeyEvent(event, isComposingRef.current)) return;
    if (event.key === "Enter") {
      event.preventDefault();
      onCommit(value);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      cancelledRef.current = true;
      onCancel();
    }
  }

  return (
    <input
      ref={inputRef}
      type="text"
      aria-label="Session name"
      data-testid="header-title-input"
      value={value}
      disabled={pending}
      onChange={(event) => setValue(event.target.value)}
      onCompositionStart={() => {
        isComposingRef.current = true;
      }}
      onCompositionEnd={() => {
        isComposingRef.current = false;
      }}
      onKeyDown={handleKeyDown}
      onBlur={() => {
        if (cancelledRef.current) return;
        onCommit(value);
      }}
      className={cn(
        // Sit flush with the static title: no vertical padding or box border,
        // just a subtle focus ring so edit mode doesn't change the title's
        // height or shift the breadcrumb.
        "min-w-0 w-56 max-w-full truncate rounded-sm bg-transparent px-1 -mx-1 py-0",
        "text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring/60",
      )}
    />
  );
}
