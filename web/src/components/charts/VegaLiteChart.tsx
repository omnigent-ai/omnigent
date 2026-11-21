import { Button } from "@/components/ui/button";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { cn } from "@/lib/utils";
import { ChartColumnIcon, CodeIcon, DownloadIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import {
  ChartDrawCancelled,
  readChartColors,
  renderVegaLiteChart,
  type RenderedChart,
} from "./vegaLiteRuntime";
import { parseVegaLiteSpec } from "./vegaLiteSpec";

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * A number that changes when the page's theme classes or palette change, so a
 * chart redraws in the new colors. The mode alone misses palette switches.
 */
function useThemeAttributesVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const observer = new MutationObserver(() => setVersion((current) => current + 1));
    observer.observe(document.documentElement, { attributeFilter: ["class", "data-theme"] });
    return () => observer.disconnect();
  }, []);
  return version;
}

type DrawState = { status: "drawing" } | { status: "drawn" } | { status: "error"; message: string };

const TOOLBAR_BUTTON_CLASS = "h-6 gap-1 px-1.5 text-xs text-muted-foreground hover:text-foreground";

/**
 * An interactive chart drawn from a Vega-Lite spec, with PNG/SVG downloads.
 *
 * A spec that can't be drawn shows why, followed by *source*.
 *
 * :param spec: The spec's JSON text.
 * :param filename: Download name without extension, e.g. ``"sales"``.
 * :param source: The spec as code, shown on demand and when drawing fails.
 *   Omit it where the surrounding view already offers the source.
 */
export function VegaLiteChart({
  spec,
  filename = "chart",
  source,
  className,
}: {
  spec: string;
  filename?: string;
  source?: ReactNode;
  className?: string;
}) {
  const parsed = useMemo(() => parseVegaLiteSpec(spec), [spec]);
  const themeMode = useResolvedThemeMode();
  const themeVersion = useThemeAttributesVersion();
  const plotRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<RenderedChart | null>(null);
  const [drawState, setDrawState] = useState<DrawState>({ status: "drawing" });
  const [showsSource, setShowsSource] = useState(false);

  useEffect(() => {
    const plot = plotRef.current;
    if (!parsed.ok || !plot) return;
    let cancelled = false;
    setDrawState({ status: "drawing" });
    renderVegaLiteChart(
      plot,
      parsed.spec,
      () => readChartColors(plot, themeMode === "dark"),
      () => cancelled,
    ).then(
      (chart) => {
        if (cancelled) {
          chart.finalize();
          return;
        }
        chartRef.current = chart;
        setDrawState({ status: "drawn" });
      },
      (error: unknown) => {
        if (cancelled || error instanceof ChartDrawCancelled) return;
        setDrawState({ status: "error", message: messageOf(error) });
      },
    );
    return () => {
      cancelled = true;
      chartRef.current?.finalize();
      chartRef.current = null;
      plot.replaceChildren();
    };
  }, [parsed, themeMode, themeVersion]);

  const download = useCallback(
    (format: "png" | "svg") => {
      const chart = chartRef.current;
      if (!chart) return;
      chart.toBlob(format).then(
        (blob) => triggerBrowserDownload(blob, `${filename}.${format}`),
        (error: unknown) => toast.error(`Couldn't save the chart: ${messageOf(error)}`),
      );
    },
    [filename],
  );

  const error = parsed.ok
    ? drawState.status === "error"
      ? drawState.message
      : null
    : parsed.error;
  const drawn = parsed.ok && drawState.status === "drawn";

  return (
    <div
      data-testid="vega-lite-chart"
      className={cn("not-prose my-4 rounded-md border bg-transparent", className)}
    >
      <div className="flex items-center gap-1 border-b px-2 py-1">
        <ChartColumnIcon aria-hidden="true" className="size-3.5 text-muted-foreground" />
        <span className="mr-auto text-xs text-muted-foreground">Chart</span>
        {source && !error && (
          <Button
            aria-pressed={showsSource}
            className={TOOLBAR_BUTTON_CLASS}
            onClick={() => setShowsSource((shows) => !shows)}
            size="sm"
            title={showsSource ? "Show chart" : "Show spec"}
            type="button"
            variant="ghost"
          >
            <CodeIcon aria-hidden="true" className="size-3.5" />
            Spec
          </Button>
        )}
        {(["png", "svg"] as const).map((format) => (
          <Button
            key={format}
            aria-label={`Download ${format.toUpperCase()}`}
            className={TOOLBAR_BUTTON_CLASS}
            disabled={!drawn}
            onClick={() => download(format)}
            size="sm"
            title={`Download ${format.toUpperCase()}`}
            type="button"
            variant="ghost"
          >
            <DownloadIcon aria-hidden="true" className="size-3.5" />
            {format.toUpperCase()}
          </Button>
        ))}
      </div>
      {error ? (
        <div className="space-y-2 p-3">
          <p data-testid="vega-lite-chart-error" className="text-sm text-destructive" role="alert">
            Couldn't draw this chart. {error}
          </p>
          {source}
        </div>
      ) : (
        showsSource && source
      )}
      {/* Stays mounted so a corrected spec can draw into it. */}
      <div className={cn("overflow-x-auto p-3", (error || showsSource) && "hidden")}>
        {drawState.status === "drawing" && (
          <p className="py-6 text-center text-xs text-muted-foreground">Drawing chart…</p>
        )}
        {/* Vega owns this element's children. */}
        <div ref={plotRef} data-testid="vega-lite-chart-plot" className="w-full" />
      </div>
    </div>
  );
}
