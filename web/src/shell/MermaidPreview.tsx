// Shared read-only mermaid renderer for the markdown preview (CodeViewer) and
// the rich-text editor (TipTapCodeBlockView), so a diagram looks identical
// whether the file is read or edited. Streamdown's mermaid plugin sanitises the
// SVG internally — the same trusted path chat messages use.

import { mermaid } from "@streamdown/mermaid";
import { Streamdown } from "streamdown";
import { MarkdownErrorBoundary } from "@/components/ai-elements/MarkdownErrorBoundary";

const MERMAID_STREAMDOWN_PLUGINS = { mermaid };

/** Render mermaid `source` (the diagram body, no fence) as an SVG diagram. */
export function MermaidPreview({ source }: { source: string }) {
  const trimmed = source.replace(/\n$/, "");
  // Fence with more backticks than any run in the source, so a ``` line inside
  // the diagram can't close the wrapper early and spill out as plain markdown.
  const ticks = (trimmed.match(/`+/g) ?? []).reduce((n, run) => Math.max(n, run.length), 2) + 1;
  const fence = "`".repeat(ticks);
  return (
    <div data-testid="mermaid-preview" className="not-prose my-4 overflow-auto">
      <MarkdownErrorBoundary source={source}>
        <Streamdown plugins={MERMAID_STREAMDOWN_PLUGINS}>
          {`${fence}mermaid\n${trimmed}\n${fence}`}
        </Streamdown>
      </MarkdownErrorBoundary>
    </div>
  );
}
