// When the open conversation's response can be stopped, as Escape in the composer stops
// it, and the ⌘K command that does the same.

import { SquareIcon } from "lucide-react";
import { useMemo, useRef } from "react";

import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import { HIDDEN, type PaletteCommand } from "@/store/paletteCommands";

export const STOP_RESPONSE_LABEL = "Stop response";

/** A turn is running and the viewer may send to the session. */
export function canStopResponse({ working, readOnly }: { working: boolean; readOnly: boolean }) {
  return working && !readOnly;
}

/** Offers Stop response in ⌘K while Escape in the composer would stop the turn. */
export function useStopResponsePaletteCommand({
  conversationId,
  working,
  readOnly,
  stop,
}: {
  conversationId: string | null;
  working: boolean;
  readOnly: boolean;
  stop: () => void;
}): void {
  const latestStop = useRef(stop);
  latestStop.current = stop;
  const stoppable = canStopResponse({ working, readOnly });
  const commands = useMemo<PaletteCommand[]>(
    () =>
      conversationId === null
        ? []
        : [
            {
              id: `conversation.stop-response:${conversationId}`,
              label: STOP_RESPONSE_LABEL,
              icon: SquareIcon,
              keywords: ["cancel", "interrupt", "halt", "turn"],
              // Listed only while a turn runs, as the composer's Stop button shows.
              availability: stoppable ? undefined : HIDDEN,
              run: () => latestStop.current(),
            },
          ],
    [conversationId, stoppable],
  );
  useQuickPickPaletteCommands(`conversation-stop:${conversationId}`, "Conversation", commands);
}
