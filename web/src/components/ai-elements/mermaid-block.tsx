import { mermaid } from "@streamdown/mermaid";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import type { CustomRendererProps } from "streamdown";

// Render completed charts while hidden and cache their SVG so expanding a
// settled trace does not start asynchronous rendering or shift its layout.
// Bound the cache because SVG strings can be tens of kilobytes each.
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
    // Mermaid needs a DOM-unique render id. Its default strict security level
    // matches the built-in renderer.
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
    // Wait for the fence to close before parsing the chart.
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
        // Mermaid generated this markup with securityLevel "strict".
        dangerouslySetInnerHTML={{ __html: svg }}
        role="img"
      />
    );
  } else {
    // Reserve space while rendering to limit layout shift.
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
