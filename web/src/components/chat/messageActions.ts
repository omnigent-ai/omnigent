// The rules a message's own buttons follow, shared with ⌘K's commands for the latest
// response and prompt, so a command and its button can't disagree.

import type { Bubble } from "@/lib/renderItems";
import type { ForkDialogContextValue } from "@/shell/ForkDialogContext";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

type AssistantBubble = Extract<Bubble, { kind: "assistant" }>;
type UserBubble = Extract<Bubble, { kind: "user" }>;

/**
 * "Fork from here" on a response: hidden when the session can't be forked or the response
 * is still streaming, greyed with the reason the session gives otherwise.
 */
export function forkFromResponseAvailability(
  fork: Pick<ForkDialogContextValue, "canFork" | "disabledReason"> | null,
  lifecycle: AssistantBubble["lifecycle"],
): CommandAvailability {
  if (!fork?.canFork || lifecycle === "streaming") return HIDDEN;
  return fork.disabledReason ? unavailableBecause(fork.disabledReason) : AVAILABLE;
}

/** The ID a user message's link points at; a message not sent yet has none. */
export function userMessageLinkId(bubble: UserBubble): string | null {
  return bubble.pending ? null : bubble.itemId;
}
