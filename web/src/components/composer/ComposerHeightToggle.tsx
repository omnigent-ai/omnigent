// The message box's corner control: it expands a long draft to a tall editor, or collapses it.

import { Maximize2Icon, Minimize2Icon } from "lucide-react";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { composerHeightActionLabel } from "@/hooks/useComposerHeightHotkey";

export function ComposerHeightToggle({
  expanded,
  onToggle,
  keys,
}: {
  expanded: boolean;
  onToggle: () => void;
  /** The chord's keys for the tooltip, where one is bound. */
  keys?: readonly string[];
}) {
  const label = composerHeightActionLabel(expanded ? "expanded" : "compact");
  const Icon = expanded ? Minimize2Icon : Maximize2Icon;
  return (
    <KeyboardShortcutTooltip label={label} keys={keys}>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label={label}
        aria-pressed={expanded}
        data-testid="composer-height-toggle"
        className="absolute top-2 right-2 z-10 text-muted-foreground hover:text-foreground"
        // A click leaves the caret where it was in the draft.
        onMouseDown={(event) => event.preventDefault()}
        onClick={onToggle}
      >
        <Icon aria-hidden />
      </Button>
    </KeyboardShortcutTooltip>
  );
}
