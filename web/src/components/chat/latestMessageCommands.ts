// ⌘K's commands for the conversation's latest response and prompt: copy either, copy a
// link to either, and fork from the response. They follow the message buttons' rules.

import type { Bubble } from "@/lib/renderItems";
import { isSystemUserContent } from "@/lib/systemMessage";
import type { ForkDialogContextValue } from "@/shell/ForkDialogContext";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

import { forkFromResponseAvailability, userMessageLinkId } from "./messageActions";

type AssistantBubble = Extract<Bubble, { kind: "assistant" }>;
type UserBubble = Extract<Bubble, { kind: "user" }>;

export const NO_RESPONSE_YET = "No response in this conversation yet.";
export const NO_RESPONSE_TEXT = "The latest response has no text to copy.";
export const NO_PROMPT_YET = "No message in this conversation yet.";
export const NO_PROMPT_TEXT = "The latest message has no text to copy.";
export const PROMPT_NOT_SENT = "Available once the message is sent.";

export type LatestMessageCommandId =
  | "copy-latest-response"
  | "copy-latest-response-link"
  | "fork-latest-response"
  | "copy-latest-prompt"
  | "copy-latest-prompt-link";

export interface LatestMessages {
  response: AssistantBubble | null;
  prompt: UserBubble | null;
}

/** Whether a user row is something the person sent: a shell command, or text that isn't a notice. */
function isPrompt(bubble: UserBubble): boolean {
  return bubble.shellCommand !== undefined || !isSystemUserContent(bubble.content);
}

/** The newest response and the newest prompt among `bubbles`. */
export function latestMessages(bubbles: readonly Bubble[]): LatestMessages {
  let response: AssistantBubble | null = null;
  let prompt: UserBubble | null = null;
  for (let index = bubbles.length - 1; index >= 0 && (!response || !prompt); index -= 1) {
    const bubble = bubbles[index]!;
    if (bubble.kind === "assistant") response ??= bubble;
    else if (bubble.kind === "user" && isPrompt(bubble)) prompt ??= bubble;
  }
  return { response, prompt };
}

export interface LatestMessageFacts {
  latest: LatestMessages;
  /** Whether the latest response has text to copy. */
  responseHasText: boolean;
  /** Whether the latest prompt has text to copy; one with only attachments has none. */
  promptHasText: boolean;
  fork: Pick<ForkDialogContextValue, "canFork" | "disabledReason"> | null;
}

export interface LatestMessageCommand {
  id: LatestMessageCommandId;
  label: string;
  availability: CommandAvailability;
}

/** The commands, in the order ⌘K lists them, with the message buttons' rules. */
export function latestMessageCommands({
  latest: { response, prompt },
  responseHasText,
  promptHasText,
  fork,
}: LatestMessageFacts): LatestMessageCommand[] {
  const needsResponse = (then: CommandAvailability) =>
    response ? then : unavailableBecause(NO_RESPONSE_YET);
  const needsPrompt = (then: CommandAvailability) =>
    prompt ? then : unavailableBecause(NO_PROMPT_YET);
  return [
    {
      id: "copy-latest-response",
      label: "Copy latest response",
      availability: needsResponse(
        responseHasText ? AVAILABLE : unavailableBecause(NO_RESPONSE_TEXT),
      ),
    },
    {
      id: "copy-latest-response-link",
      label: "Copy link to latest response",
      availability: needsResponse(AVAILABLE),
    },
    {
      id: "fork-latest-response",
      label: "Fork from latest response…",
      availability: response
        ? forkFromResponseAvailability(fork, response.lifecycle)
        : fork?.canFork
          ? unavailableBecause(NO_RESPONSE_YET)
          : HIDDEN,
    },
    {
      id: "copy-latest-prompt",
      label: "Copy latest prompt",
      availability: needsPrompt(promptHasText ? AVAILABLE : unavailableBecause(NO_PROMPT_TEXT)),
    },
    {
      id: "copy-latest-prompt-link",
      label: "Copy link to latest prompt",
      availability: needsPrompt(
        prompt && userMessageLinkId(prompt) === null
          ? unavailableBecause(PROMPT_NOT_SENT)
          : AVAILABLE,
      ),
    },
  ];
}
