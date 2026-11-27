// The ⌘K entry for "Resume sandbox". Its label, availability, and action are the
// ones every other Resume surface uses.

import { PlayIcon } from "lucide-react";
import { useMemo } from "react";

import { useSandboxResume } from "@/hooks/useSandboxResume";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
} from "@/store/paletteCommands";

import { useCommandContext } from "./commandContext";
import { RESUME_SANDBOX_KEYWORDS, RESUME_SANDBOX_LABEL } from "./resumeSandboxAvailability";
import { useQuickPickPaletteCommands } from "./screenCommands/useQuickPickPaletteCommands";

/** The command for `sessionId`'s sandbox, while it has one to resume; greyed out for non-owners. */
export function resumeSandboxPaletteCommands(
  sessionId: string | null,
  availability: CommandAvailability,
  resume: () => void,
): PaletteCommand[] {
  if (sessionId === null || !isCommandListed(availability)) return [];
  return [
    {
      // The session is part of the id, so a row accepted after switching sessions does nothing.
      id: `conversation.resume-sandbox:${sessionId}`,
      label: RESUME_SANDBOX_LABEL,
      icon: PlayIcon,
      keywords: RESUME_SANDBOX_KEYWORDS,
      availability,
      run: resume,
    },
  ];
}

/** Offers Resume sandbox in ⌘K for the open session while its sandbox is asleep or failed. */
export function ResumeSandboxPaletteCommand(): null {
  const sessionId = useCommandContext().session?.id ?? null;
  const { availability, resume } = useSandboxResume(sessionId);
  const listed = isCommandListed(availability);
  const reason = availability.available ? undefined : availability.reason;
  // Rebuilt from its parts, so an equal availability doesn't re-register the row each render.
  const commands = useMemo(() => {
    const offered = !listed
      ? HIDDEN
      : reason === undefined
        ? AVAILABLE
        : unavailableBecause(reason);
    return resumeSandboxPaletteCommands(sessionId, offered, resume);
  }, [sessionId, listed, reason, resume]);
  useQuickPickPaletteCommands("conversation-resume-sandbox", "Conversation", commands);
  return null;
}
