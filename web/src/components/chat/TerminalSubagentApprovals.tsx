import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import { ElicitationCard } from "@/components/blocks/ApprovalCard";
import { isOwnElicitation, type AnyBlock, type ElicitationBlock } from "@/lib/blocks";
import { elicitationRenderItem } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";
import { useChatStore } from "@/store/chatStore";

/** Pending prompts that a sub-agent mirrored into this session's stream. */
export function pendingSubagentElicitations(
  blocks: readonly AnyBlock[],
  conversationId: string | null,
): ElicitationBlock[] {
  return blocks.filter(
    (block): block is ElicitationBlock =>
      block.type === "elicitation" &&
      block.status === "pending" &&
      !isOwnElicitation(block, conversationId),
  );
}

/** Sub-agent approval cards for the terminal view, which hides the transcript. */
export function TerminalSubagentApprovals() {
  const pendingBlocks = useChatStore(
    useShallow((s) => pendingSubagentElicitations(s.blocks, s.conversationId)),
  );
  const items = useMemo(() => pendingBlocks.map(elicitationRenderItem), [pendingBlocks]);
  if (items.length === 0) return null;
  return (
    <div
      data-testid="terminal-subagent-approvals"
      className={cn(
        "mx-auto flex max-h-[50%] w-full shrink-0 flex-col gap-2 overflow-y-auto px-4 pt-2 pb-3",
        CHAT_COLUMN_WIDTH,
      )}
    >
      {items.map((item) => (
        <ElicitationCard key={item.elicitationId} item={item} />
      ))}
    </div>
  );
}
