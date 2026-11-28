import { FileDiffIcon, LayoutListIcon, MessageSquareIcon } from "lucide-react";
import { useId, useMemo, useRef } from "react";

import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import {
  type SectionCommandFacts,
  type SectionCommandId,
  type SectionCommandTarget,
  sectionCommands,
} from "./sectionCommands";

const ICONS: Record<SectionCommandId, PaletteCommand["icon"]> = {
  "show-overview": LayoutListIcon,
  "show-comments": MessageSquareIcon,
  "show-changes": FileDiffIcon,
};

const NO_COMMANDS: PaletteCommand[] = [];

/** Offer the section commands in ⌘K while `active`; each instance offers only its own. */
export function useSectionCommands({
  conversationId,
  active,
  facts,
  show,
}: {
  conversationId: string;
  /** False while the panel is hidden. */
  active: boolean;
  facts: SectionCommandFacts;
  /** Show a section and move focus to its control. */
  show: (section: SectionCommandTarget) => void;
}): void {
  const latest = useRef(show);
  latest.current = show;
  const { available, shown, commitOpen } = facts;
  const commands = useMemo<PaletteCommand[]>(
    () =>
      sectionCommands({ available, shown, commitOpen }).map((command) => ({
        id: command.id,
        label: command.paletteLabel,
        icon: ICONS[command.id],
        keywords: [...command.keywords],
        // Focus goes to the section's control once ⌘K has closed.
        movesFocus: true,
        run: () => latest.current(command.target),
      })),
    [available, shown, commitOpen],
  );
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-sections:${conversationId}:${paletteInstance}`,
    "Pull request",
    active ? commands : NO_COMMANDS,
  );
}
