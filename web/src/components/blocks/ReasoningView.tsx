// Adapter from a `RenderItem.reasoning` to the vendored `<Reasoning>`
// chain. The vendored component handles auto-open/auto-close timing and
// the "Thought for N seconds" timer via wall clock; we just thread
// through `isStreaming` and the accumulated text.

import { Reasoning, ReasoningContent, ReasoningTrigger } from "@/components/ai-elements/reasoning";
import { firstLine } from "@/lib/toolValue";

interface ReasoningViewProps {
  text: string;
  isStreaming: boolean;
  duration: number | undefined;
}

// Code fences and thematic breaks carry no prose worth previewing.
const MARKUP_ONLY_LINE_RE = /^(?:`{3,}|~{3,}|[-*_](?:\s*[-*_]){2,}$)/;
// Leading heading, blockquote, and list markers on the preview line.
const LEADING_BLOCK_MARKERS_RE = /^(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+/;
// `**` only: `__` would mangle dunder names like `__init__.py`.
const STRONG_RE = /\*\*(.+?)\*\*/g;
const INLINE_CODE_RE = /`([^`]+)`/g;

/** First prose line of a thought as plain text, e.g. `"**Planning** the fix"` -> `"Planning the fix"`. */
function reasoningPreview(text: string): string {
  for (const rawLine of text.split("\n")) {
    const line = firstLine(rawLine);
    if (line.length === 0 || MARKUP_ONLY_LINE_RE.test(line)) continue;
    const preview = line
      .replace(LEADING_BLOCK_MARKERS_RE, "")
      .replace(STRONG_RE, "$1")
      .replace(INLINE_CODE_RE, "$1")
      .trim();
    if (preview.length > 0) return preview;
  }
  return "";
}

export function ReasoningView({ text, isStreaming, duration }: ReasoningViewProps) {
  // A reasoning section is only worth expanding when it has something to
  // show. While streaming we keep it expandable even before the first
  // chunk lands (the "Thinking..." shimmer is live feedback, and content
  // is on its way). Once settled, an empty section (e.g. a `reasoning_start`
  // with no chunks) renders as a flat header with no dead expand affordance.
  const expandable = isStreaming || text.trim().length > 0;
  // A settled thought previews its first line so successive thoughts are
  // distinguishable; empty thoughts keep the "Thought for ..." fallback.
  const preview = isStreaming ? "" : reasoningPreview(text);
  const getThinkingMessage = preview
    ? () => (
        <span>
          <strong className="font-semibold">Thinking:</strong> {preview}
        </span>
      )
    : undefined;
  return (
    <Reasoning isStreaming={isStreaming} duration={duration} expandable={expandable}>
      <ReasoningTrigger getThinkingMessage={getThinkingMessage} />
      <ReasoningContent>{text}</ReasoningContent>
    </Reasoning>
  );
}
