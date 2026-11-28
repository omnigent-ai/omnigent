import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import { useMemo, useRef, useId } from "react";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

export const PREVIOUS_UNRESOLVED_CONVERSATION_LABEL = "Go to the previous unresolved conversation";
export const NEXT_UNRESOLVED_CONVERSATION_LABEL = "Go to the next unresolved conversation";

/** ⌘K moves between unresolved conversations as the Changes tab's arrows do, opening that tab. */
export function UnresolvedConversationPaletteCommands({
  source,
  active,
  unresolvedCount,
  openChanges,
  navigate,
}: {
  source: string;
  active: boolean;
  unresolvedCount: number;
  openChanges: () => void;
  navigate: (direction: number) => void;
}): null {
  const actionsRef = useRef({ openChanges, navigate });
  actionsRef.current = { openChanges, navigate };
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!active || unresolvedCount === 0) return [];
    const go = (direction: number) => {
      actionsRef.current.openChanges();
      actionsRef.current.navigate(direction);
    };
    return [
      {
        id: "previous-unresolved-conversation",
        label: PREVIOUS_UNRESOLVED_CONVERSATION_LABEL,
        // Focuses the thread, so it runs after the palette hands focus back.
        movesFocus: true,
        icon: ChevronUpIcon,
        keywords: ["pull request", "review", "comment", "thread", "unresolved", "previous"],
        run: () => go(-1),
      },
      {
        id: "next-unresolved-conversation",
        label: NEXT_UNRESOLVED_CONVERSATION_LABEL,
        movesFocus: true,
        icon: ChevronDownIcon,
        keywords: ["pull request", "review", "comment", "thread", "unresolved", "next"],
        run: () => go(1),
      },
    ];
  }, [active, unresolvedCount]);
  // Scoped to this instance, so another mounted tab's rows for the same pull request stay.
  const instance = useId();
  usePaletteCommands(`${source}:${instance}`, "Pull request", commands);
  return null;
}
