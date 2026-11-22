import { Button } from "@/components/ui/button";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import type { PastedTextHint } from "@/hooks/usePastedTextAttachments";
import { pastedLinesLabel } from "@/hooks/usePastedTextCommands";
import { isMacPlatform } from "@/lib/hotkeys";
import { ComposerFeedbackRow } from "./ChatComposer";

/** Says a large paste became an attachment, with the one step back to inline text. */
export function PastedTextHintRow({
  hint,
  disabled = false,
  onInsertAsText,
}: {
  hint: PastedTextHint;
  /** The draft is locked; the chip stays until it can be restored. */
  disabled?: boolean;
  onInsertAsText: () => void;
}) {
  const lines = pastedLinesLabel(hint.lines);
  return (
    <ComposerFeedbackRow
      className="flex flex-wrap items-center gap-x-2 gap-y-1"
      data-testid="composer-pasted-text-hint"
      role="status"
    >
      <span>Large paste ({lines}) added as an attachment.</span>
      {/* The action and its shortcut wrap as one, so the keys never sit alone on a line. */}
      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-auto p-0 text-sm font-medium text-foreground"
          // Keep focus in the message so typing carries on where it was.
          onMouseDown={(event) => event.preventDefault()}
          onClick={onInsertAsText}
          disabled={disabled}
        >
          Insert as text
        </Button>
        {hint.undoable && !disabled && (
          <CompactShortcutKeys keys={isMacPlatform() ? ["⌘", "Z"] : ["Ctrl", "Z"]} />
        )}
      </span>
    </ComposerFeedbackRow>
  );
}
