// The open session's actions in ⌘K: the same set, labels and gates as the header's session
// menu, which offers them to the session's owner and runs them with its own handlers.

import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  ClockPlusIcon,
  DownloadIcon,
  GitForkIcon,
  type LucideIcon,
  MailIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  ShareIcon,
  Trash2Icon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import type { Chord } from "@/lib/shortcuts/chord";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";

export type OpenSessionCommandId =
  | "add-nested-agent"
  | "pin"
  | "unpin"
  | "share"
  | "fork"
  | "export"
  | "rename"
  | "mark-unread"
  | "create-automation"
  | "archive"
  | "unarchive"
  | "delete";

/** What the header menu knows about the open session when it builds its items. */
export interface OpenSessionFacts {
  canAddAgent: boolean;
  /** `false` for a family member shown under its leader; it pins with the family. */
  pinnable: boolean;
  pinned: boolean;
  /** A pin write is saving; pin writes don't overlap. */
  pinSaving: boolean;
  canShare: boolean;
  /** Why Share is offered but can't run here, such as sharing turned off. */
  shareDisabledReason?: string;
  canFork: boolean;
  forkDisabledReason?: string;
  /** A muted session can't be flagged unread. */
  canMarkUnread: boolean;
  canCreateAutomation: boolean;
  archived: boolean;
  /** The desktop shell binds Pin, Fork and Archive in its menu bar. */
  desktopShortcuts: boolean;
}

export interface OpenSessionCommand {
  id: OpenSessionCommandId;
  label: string;
  keywords: string[];
  icon: LucideIcon;
  availability: CommandAvailability;
  destructive?: boolean;
  nativeShortcut?: Chord;
}

const PIN_CHORD: Chord = { mod: true, alt: true, code: "KeyP" };
const FORK_CHORD: Chord = { mod: true, alt: true, code: "KeyF" };
const ARCHIVE_CHORD: Chord = { mod: true, shift: true, key: "a" };
const PIN_SAVING_REASON = "A pin change is still saving.";

function offeredUnless(offered: boolean, disabledReason?: string): CommandAvailability {
  if (!offered) return HIDDEN;
  return disabledReason ? unavailableBecause(disabledReason) : AVAILABLE;
}

/** The open session's commands in the header menu's order; hidden ones stay out of ⌘K. */
export function openSessionCommands(facts: OpenSessionFacts): OpenSessionCommand[] {
  const shortcut = (chord: Chord) => (facts.desktopShortcuts ? chord : undefined);
  const commands: OpenSessionCommand[] = [
    {
      id: "add-nested-agent",
      label: "Add nested agent",
      keywords: ["sibling", "session", "agent"],
      icon: PlusIcon,
      availability: offeredUnless(facts.canAddAgent),
    },
    {
      id: facts.pinned ? "unpin" : "pin",
      label: facts.pinned ? "Unpin session" : "Pin session",
      keywords: ["pin", "session"],
      icon: facts.pinned ? PinOffIcon : PinIcon,
      availability: offeredUnless(facts.pinnable, facts.pinSaving ? PIN_SAVING_REASON : undefined),
      nativeShortcut: shortcut(PIN_CHORD),
    },
    {
      id: "share",
      label: "Share session",
      keywords: ["share", "permissions", "invite"],
      icon: ShareIcon,
      availability: offeredUnless(facts.canShare, facts.shareDisabledReason),
    },
    {
      id: "fork",
      label: "Fork session",
      keywords: ["fork", "copy", "branch"],
      icon: GitForkIcon,
      availability: offeredUnless(facts.canFork, facts.forkDisabledReason),
      nativeShortcut: shortcut(FORK_CHORD),
    },
    {
      id: "export",
      label: "Export session",
      keywords: ["export", "download", "transcript"],
      icon: DownloadIcon,
      availability: AVAILABLE,
    },
    {
      id: "rename",
      label: "Rename session",
      keywords: ["rename", "title"],
      icon: PencilIcon,
      availability: AVAILABLE,
    },
    {
      id: "mark-unread",
      label: "Mark session as unread",
      keywords: ["unread", "session"],
      icon: MailIcon,
      availability: offeredUnless(facts.canMarkUnread),
    },
    {
      id: "create-automation",
      label: "Create automation from session",
      keywords: ["automation", "schedule"],
      icon: ClockPlusIcon,
      availability: offeredUnless(facts.canCreateAutomation),
    },
    {
      id: facts.archived ? "unarchive" : "archive",
      label: facts.archived ? "Unarchive session" : "Archive session",
      keywords: ["archive", "session"],
      icon: facts.archived ? ArchiveRestoreIcon : ArchiveIcon,
      availability: AVAILABLE,
      nativeShortcut: shortcut(ARCHIVE_CHORD),
    },
    {
      id: "delete",
      label: "Delete session",
      keywords: ["delete", "remove", "session"],
      icon: Trash2Icon,
      availability: AVAILABLE,
      destructive: true,
    },
  ];
  return commands.filter((command) => isCommandListed(command.availability));
}

/** Offer the open session's commands under "Session" while its header menu is mounted. */
export function useOpenSessionPaletteCommands({
  conversationId,
  facts,
  onRun,
}: {
  conversationId: string;
  facts: OpenSessionFacts;
  onRun: (id: OpenSessionCommandId) => void;
}): void {
  const runRef = useRef(onRun);
  runRef.current = onRun;
  const commands = openSessionCommands(facts);
  // Rebuilt only when what the rows say changes, not on every render of the header.
  const signature = JSON.stringify(commands.map(({ icon: _icon, ...row }) => row));
  const commandsRef = useRef(commands);
  commandsRef.current = commands;
  const shownSessionRef = useRef(conversationId);
  shownSessionRef.current = conversationId;
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      commandsRef.current.map((command) => ({
        // The session is part of the id, so a row built for another session never runs.
        id: `open-session:${conversationId}:${command.id}`,
        label: command.label,
        icon: command.icon,
        keywords: command.keywords,
        availability: command.availability,
        destructive: command.destructive,
        nativeShortcut: command.nativeShortcut,
        // Dialogs such as Rename take focus once the palette has closed.
        movesFocus: true,
        // Read again at run time: a caller may hold this row past a change in the session.
        run: () => {
          const current = commandsRef.current.find((each) => each.id === command.id);
          if (
            shownSessionRef.current === conversationId &&
            current &&
            isCommandAvailable(current.availability)
          ) {
            runRef.current(command.id);
          }
        },
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the signature covers `commands`.
    [conversationId, signature],
  );
  usePaletteCommands(`open-session:${conversationId}`, "Session", paletteCommands);
}
