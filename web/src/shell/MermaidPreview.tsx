// Shared read-only mermaid renderer for the markdown preview (CodeViewer) and
// the rich-text editor (TipTapCodeBlockView), so a diagram looks identical
// whether the file is read or edited. Streamdown's mermaid plugin sanitises the
// SVG internally — the same trusted path chat messages use.

import { mermaid } from "@streamdown/mermaid";
import { Streamdown } from "streamdown";
import { MarkdownErrorBoundary } from "@/components/ai-elements/MarkdownErrorBoundary";
import { fenceForBody } from "./markdownFence";

const MERMAID_STREAMDOWN_PLUGINS = { mermaid };

/** Render mermaid `source` (the diagram body, no fence) as an SVG diagram. */
export function MermaidPreview({ source }: { source: string }) {
  const trimmed = source.replace(/\n$/, "");
  // Fence with more backticks than any run in the source, so a ``` line inside
  // the diagram can't close the wrapper early and spill out as plain markdown.
  const fence = fenceForBody(trimmed);
  return (
    <div data-testid="mermaid-preview" className="not-prose my-4 overflow-auto">
      <MarkdownErrorBoundary source={source}>
        {/* key by source: mermaid renders async with no cancellation, so remount
            on each settled source to discard a superseded in-flight render. */}
        <Streamdown key={trimmed} plugins={MERMAID_STREAMDOWN_PLUGINS}>
          {`${fence}mermaid\n${trimmed}\n${fence}`}
        </Streamdown>
      </MarkdownErrorBoundary>
    </div>
  );
}
