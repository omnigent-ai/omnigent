// Adapter from a `RenderItem.reasoning` to the vendored `<Reasoning>`
// chain. The vendored component handles auto-open/auto-close timing and
// the "Thought for N seconds" timer via wall clock; we just thread
// through `isStreaming` and the accumulated text.

import { Reasoning, ReasoningContent, ReasoningTrigger } from "@/components/ai-elements/reasoning";
import { FIND_CURRENT_ATTRIBUTE, type FindReveal } from "@/components/chat/chatFindReveal";

interface ReasoningViewProps {
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
  /** Set while find in chat shows a match inside this section. */
  findReveal?: FindReveal;
}

export function ReasoningView({ text, isStreaming, duration, findReveal }: ReasoningViewProps) {
  // A reasoning section is only worth expanding when it has something to
  // show. While streaming we keep it expandable even before the first
  // chunk lands (the "Thinking..." shimmer is live feedback, and content
  // is on its way). Once settled, an empty section (e.g. a `reasoning_start`
  // with no chunks) renders as a flat header with no dead expand affordance.
  const expandable = isStreaming || text.trim().length > 0;
  return (
    <Reasoning
      isStreaming={isStreaming}
      duration={duration}
      expandable={expandable}
      revealed={findReveal !== undefined}
      {...{ [FIND_CURRENT_ATTRIBUTE]: findReveal === "current" ? "" : undefined }}
    >
      <ReasoningTrigger />
      <ReasoningContent>{text}</ReasoningContent>
    </Reasoning>
  );
}
