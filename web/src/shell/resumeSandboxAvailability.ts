// When "Resume sandbox" can run, shared by ⌘K, the composer's host menu, and
// every workspace panel a stopped sandbox blocks, so they can't disagree.
// Only the session's owner resumes its sandbox; everyone else sees it read-only.

import type { SandboxResumeStatus } from "@/lib/sandboxResume";
import { HIDDEN, type CommandAvailability } from "@/store/paletteCommands";

import { type CommandSession, sessionCommandAvailability } from "./commandContext";

export const RESUME_SANDBOX_LABEL = "Resume sandbox";
export const RESUME_SANDBOX_KEYWORDS = [
  "wake",
  "start",
  "sandbox",
  "reconnect",
  "offline",
  "asleep",
];
export const RESUME_SANDBOX_NEEDS_OWNER = "Only the session’s owner can resume its sandbox.";

/** Offered while the sandbox is asleep or its last resume failed; hidden otherwise. */
export function resumeSandboxAvailability(
  session: CommandSession | null,
  status: SandboxResumeStatus,
): CommandAvailability {
  if (status !== "asleep" && status !== "failed") return HIDDEN;
  return sessionCommandAvailability(session, "owner", RESUME_SANDBOX_NEEDS_OWNER);
}
