import { SuggestionChart } from "@/components/inbox/SuggestionChart";
import type { SuggestionMediaBlock } from "@/lib/proactiveInbox";

/**
 * Draws a suggestion's rich-content blocks in order. A block type this build
 * does not know is skipped, so the server can add types before every client
 * can draw them.
 */
export function SuggestionMedia({ media }: { media: SuggestionMediaBlock[] }) {
  return (
    <>
      {media.map((block, index) =>
        block.type === "chart" ? (
          // Blocks have no ids, and their order never changes.
          // oxlint-disable-next-line react/no-array-index-key
          <SuggestionChart key={index} chart={block.chart} />
        ) : null,
      )}
    </>
  );
}
