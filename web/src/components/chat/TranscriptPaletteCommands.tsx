// The open conversation's ⌘K commands, with quick_pick_palette on: its latest response and
// prompt, and Scroll to newest message. Rendered inside the transcript's scroll container.

import {
  ArrowDownIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  CopyIcon,
  Link2Icon,
  type LucideIcon,
  SplitIcon,
} from "lucide-react";
import { useMemo, useRef } from "react";
import { useStickToBottomContext } from "use-stick-to-bottom";

import { showToast } from "@/components/ui/toast";
import type { UserMessageNav } from "@/hooks/useUserMessageNav";
import { copyText } from "@/lib/clipboard";
import { copyMarkdown } from "@/lib/copyMarkdown";
import { buildMessageDeepLink } from "@/lib/messageDeepLink";
import type { Bubble } from "@/lib/renderItems";
import {
  useQuickPickPaletteCommands,
  useQuickPickPaletteEnabled,
} from "@/shell/screenCommands/useQuickPickPaletteCommands";
import { useForkDialog } from "@/shell/ForkDialogContext";
import { AVAILABLE, type PaletteCommand, unavailableBecause } from "@/store/paletteCommands";

import { collectBubbleMarkdown, userBubbleCopyText } from "./chatBubbleParts";
import {
  type LatestMessageCommandId,
  latestMessageCommands,
  latestMessages,
  NO_PROMPT_YET,
} from "./latestMessageCommands";
import { userMessageLinkId } from "./messageActions";

export const SCROLL_TO_NEWEST_LABEL = "Scroll to newest message";
export const AT_NEWEST_MESSAGE = "The newest message is already showing.";

const ICONS: Record<LatestMessageCommandId, LucideIcon> = {
  "copy-latest-response": CopyIcon,
  "copy-latest-response-link": Link2Icon,
  "fork-latest-response": SplitIcon,
  "copy-latest-prompt": CopyIcon,
  "copy-latest-prompt-link": Link2Icon,
};

function copied(write: Promise<void>, message: string): void {
  write.then(
    () => showToast(<span className="text-ui">{message}</span>, { duration: 1500 }),
    (error: unknown) => console.warn("Failed to copy from ⌘K", error),
  );
}

interface TranscriptPaletteCommandsProps {
  conversationId: string | null;
  bubbles: readonly Bubble[];
  /** The transcript's own message steps, which its ⌘⌥↑ and ⌘⌥↓ run. */
  messageNav?: Pick<UserMessageNav, "goPrev" | "goNext">;
}

/** Does no work per streamed frame with the flag off. */
export function TranscriptPaletteCommands(props: TranscriptPaletteCommandsProps) {
  return useQuickPickPaletteEnabled() ? <ConversationCommands {...props} /> : null;
}

function ConversationCommands({
  conversationId,
  bubbles,
  messageNav,
}: TranscriptPaletteCommandsProps) {
  const fork = useForkDialog();
  const { isAtBottom, scrollToBottom } = useStickToBottomContext();
  const latest = latestMessages(bubbles);
  const commands = latestMessageCommands({
    latest,
    // Checked without building the text, since this runs on every streamed frame.
    responseHasText:
      latest.response?.items.some((item) => item.kind === "text" && item.text.trim() !== "") ??
      false,
    promptHasText: latest.prompt ? userBubbleCopyText(latest.prompt) !== "" : false,
    fork,
  });
  // Read when run, so a command copies the message showing then, not one from a frame ago.
  const current = useRef({ latest, fork, messageNav });
  current.current = { latest, fork, messageNav };
  const hasMessageNav = messageNav !== undefined;
  // The transcript re-renders per streamed frame; the rows change only when this does.
  const signature = JSON.stringify(commands.map(({ id, availability }) => [id, availability]));
  const paletteCommands = useMemo<PaletteCommand[]>(
    () => [
      ...commands.map((command) => ({
        id: `conversation.${command.id}:${conversationId}`,
        label: command.label,
        icon: ICONS[command.id],
        keywords: ["message", "conversation", "latest", "last"],
        availability: command.availability,
        movesFocus: command.id === "fork-latest-response",
        run: () => {
          const { latest: now, fork: forkDialog } = current.current;
          const { response, prompt } = now;
          switch (command.id) {
            case "copy-latest-response":
              if (response)
                copied(copyMarkdown(collectBubbleMarkdown(response.items)), "Copied to clipboard");
              return;
            case "copy-latest-response-link":
              if (response)
                copied(copyText(buildMessageDeepLink(response.responseId)), "Link copied");
              return;
            case "fork-latest-response":
              if (response) forkDialog?.openForkDialog({ upToResponseId: response.responseId });
              return;
            case "copy-latest-prompt": {
              // As the message's own button, never overwrite the clipboard with nothing.
              const text = prompt ? userBubbleCopyText(prompt) : "";
              if (text) copied(copyText(text), "Copied to clipboard");
              return;
            }
            case "copy-latest-prompt-link": {
              const linkId = prompt ? userMessageLinkId(prompt) : null;
              if (linkId) copied(copyText(buildMessageDeepLink(linkId)), "Link copied");
              return;
            }
          }
        },
      })),
      ...(hasMessageNav
        ? (["previous", "next"] as const).map(
            (direction) =>
              ({
                id: `conversation.${direction}-message:${conversationId}`,
                label: direction === "previous" ? "Go to previous message" : "Go to next message",
                icon: direction === "previous" ? ChevronUpIcon : ChevronDownIcon,
                keywords: ["message", "jump", "scroll", "prompt", "step"],
                shortcut: direction === "previous" ? "message.previous" : "message.next",
                availability: latest.prompt ? AVAILABLE : unavailableBecause(NO_PROMPT_YET),
                run: () => {
                  const nav = current.current.messageNav;
                  if (direction === "previous") nav?.goPrev();
                  else nav?.goNext();
                },
              }) satisfies PaletteCommand,
          )
        : []),
      {
        id: `conversation.scroll-to-newest:${conversationId}`,
        label: SCROLL_TO_NEWEST_LABEL,
        icon: ArrowDownIcon,
        keywords: ["bottom", "end", "latest", "jump"],
        availability: isAtBottom ? unavailableBecause(AT_NEWEST_MESSAGE) : AVAILABLE,
        // As the transcript's arrow button scrolls.
        run: () => void scrollToBottom("instant"),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the signature covers `commands`.
    [conversationId, signature, isAtBottom, scrollToBottom, hasMessageNav],
  );
  useQuickPickPaletteCommands(
    `conversation:${conversationId}`,
    "Conversation",
    conversationId ? paletteCommands : NO_COMMANDS,
  );
  return null;
}

const NO_COMMANDS: PaletteCommand[] = [];
