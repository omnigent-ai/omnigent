// Marks a maximized terminal pane in its top-right corner; clicking it shows the whole
// split again. It never takes the keyboard from the terminal under it on a click.

import { Minimize2Icon } from "lucide-react";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { PANE_ZOOM_LABELS } from "./paneCommandTable";
import { paneShortcutAria, paneShortcutKeys } from "./paneStrip/paneShortcutHints";

export function TerminalZoomPill({ onUnzoom }: { onUnzoom: () => void }) {
  return (
    <KeyboardShortcutTooltip
      label={PANE_ZOOM_LABELS.restore}
      keys={paneShortcutKeys("terminal.toggleZoom")}
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        data-terminal-zoom-pill=""
        aria-label={PANE_ZOOM_LABELS.restore}
        aria-keyshortcuts={paneShortcutAria("terminal.toggleZoom")}
        // A click leaves focus in the terminal; Enter or Space on the pill hands it back.
        onMouseDown={(event) => event.preventDefault()}
        onClick={onUnzoom}
        className="absolute top-2 right-3 z-10 bg-background/90 font-medium text-foreground shadow-sm backdrop-blur-sm"
      >
        <Minimize2Icon />
        {PANE_ZOOM_LABELS.maximized}
      </Button>
    </KeyboardShortcutTooltip>
  );
}
