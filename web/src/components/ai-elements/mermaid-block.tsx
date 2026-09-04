import { mermaid } from "@streamdown/mermaid";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { CustomRendererProps } from "streamdown";

// Chat-side renderer for ```mermaid fences, registered through Streamdown's
// custom-renderer seam (`plugins.renderers`), which takes precedence over
// the built-in mermaid block.
//
// The built-in block gates rendering behind an IntersectionObserver and
// renders from scratch on every mount. Both interact badly with the
// settled-turn fold, which keeps its trace mounted but hidden while
// collapsed: hidden content never intersects, so a folded diagram only
// started rendering AFTER the user expanded the fold — its reserved
// placeholder swapped to a spinner and then to the full-size diagram,
// jolting everything below by hundreds of pixels. This renderer starts
// rendering as soon as the fence is complete (mermaid renders in a
// detached container, so the caller's visibility is irrelevant) and caches
// the SVG per chart, so remounts paint the finished diagram on their first
// frame.
//
// Presentation mirrors the built-in block's frame (header + bordered
// body); the built-in pan-zoom / fullscreen / image-export extras are
// deliberately not reproduced.

// Rendered-SVG cache: a chart is immutable once its fence closes, and SVG
// strings can be tens of kilobytes, so cap the cache and evict the oldest
// entry (Map preserves insertion order) instead of growing unbounded.
const SVG_CACHE = new Map<string, string>();
const SVG_CACHE_MAX_ENTRIES = 64;

const inflightRenders = new Map<string, Promise<string>>();
let renderSequence = 0;

function rememberSvg(chart: string, svg: string): void {
  SVG_CACHE.delete(chart);
  SVG_CACHE.set(chart, svg);
  if (SVG_CACHE.size > SVG_CACHE_MAX_ENTRIES) {
    const oldest = SVG_CACHE.keys().next().value;
    if (oldest !== undefined) SVG_CACHE.delete(oldest);
  }
}

async function renderChartUncached(chart: string): Promise<string> {
  try {
    renderSequence += 1;
    // Render ids must be DOM-unique: mermaid renders into a temporary
    // element keyed on the id. The plugin's instance keeps its defaults
    // (securityLevel "strict"), matching the built-in block.
    const { svg } = await mermaid.getMermaid().render(`chat-mermaid-${renderSequence}`, chart);
    rememberSvg(chart, svg);
    return svg;
  } finally {
    inflightRenders.delete(chart);
  }
}

function renderChart(chart: string): Promise<string> {
  const cached = SVG_CACHE.get(chart);
  if (cached !== undefined) return Promise.resolve(cached);
  const pending = inflightRenders.get(chart) ?? renderChartUncached(chart);
  inflightRenders.set(chart, pending);
  return pending;
}

export function ChatMermaidBlock({ code, isIncomplete }: CustomRendererProps) {
  const [svg, setSvg] = useState<string | null>(() =>
    isIncomplete ? null : (SVG_CACHE.get(code) ?? null),
  );
  const [renderError, setRenderError] = useState<string | null>(null);

  useEffect(() => {
    // A still-streaming fence is rarely a parseable diagram; wait for it
    // to close instead of burning renders (and error states) on prefixes.
    if (isIncomplete) return undefined;
    const cached = SVG_CACHE.get(code);
    if (cached !== undefined) {
      setSvg(cached);
      setRenderError(null);
      return undefined;
    }
    let cancelled = false;
    const run = async () => {
      try {
        const rendered = await renderChart(code);
        if (cancelled) return;
        setSvg(rendered);
        setRenderError(null);
      } catch (error) {
        if (cancelled) return;
        setSvg(null);
        setRenderError(error instanceof Error ? error.message : "Failed to render Mermaid chart");
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [code, isIncomplete]);

  let body: ReactNode;
  if (renderError !== null) {
    body = (
      <div className="p-4">
        <p className="font-mono text-destructive text-sm">Mermaid error: {renderError}</p>
        <pre className="mt-2 overflow-x-auto text-muted-foreground text-xs">{code}</pre>
      </div>
    );
  } else if (svg !== null) {
    body = (
      <div
        aria-label="Mermaid chart"
        className="flex justify-center overflow-x-auto p-2"
        // The markup comes from mermaid itself (securityLevel "strict"),
        // injected exactly the way the built-in block injects it.
        dangerouslySetInnerHTML={{ __html: svg }}
        role="img"
      />
    );
  } else {
    // Reserve the built-in placeholder's footprint so a diagram that
    // finishes rendering while visible shifts surrounding layout as
    // little as possible.
    body = <div aria-hidden className="min-h-[200px]" />;
  }

  return (
    <div
      className="my-4 flex w-full flex-col gap-2 rounded-xl border border-border bg-sidebar p-2"
      data-testid="chat-mermaid-block"
    >
      <div className="flex h-8 items-center text-muted-foreground text-xs">
        <span className="ml-1 font-mono lowercase">mermaid</span>
      </div>
      <div className="rounded-md border border-border bg-background">{body}</div>
    </div>
  );
}
