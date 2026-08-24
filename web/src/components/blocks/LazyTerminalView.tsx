// Lazy boundary in front of `TerminalView`.
//
// `TerminalView` pulls in xterm.js and its fit / web-links / WebGL addons —
// together the single largest dependency in the app. Every mount site renders
// it only once a terminal is actually on screen, so importing it eagerly meant
// every page load paid for a terminal most sessions never open. Behind
// `React.lazy` the chunk is fetched on the first terminal render instead.
//
// Import this rather than `./TerminalView` from anything that renders a
// terminal; a static import anywhere on the eager graph pulls xterm back into
// the entry chunk.

import { lazy, Suspense, type ComponentProps } from "react";
import { Loader2Icon } from "lucide-react";
import type { TerminalView as TerminalViewImpl } from "./TerminalView";

const TerminalViewChunk = lazy(() =>
  import("./TerminalView").then((m) => ({ default: m.TerminalView })),
);

export type TerminalViewProps = ComponentProps<typeof TerminalViewImpl>;

/**
 * `TerminalView`, loaded on demand.
 *
 * The fallback mirrors the terminal's own "Connecting…" overlay, so a cold
 * chunk load reads as part of the connect it precedes rather than as a
 * separate loading state.
 */
export function LazyTerminalView(props: TerminalViewProps) {
  return (
    <Suspense
      fallback={
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div className="absolute inset-0 flex items-center justify-center bg-background/85 text-ui text-foreground">
            <span className="flex items-center gap-2">
              <Loader2Icon className="size-4 animate-spin" />
              Connecting…
            </span>
          </div>
        </div>
      }
    >
      <TerminalViewChunk {...props} />
    </Suspense>
  );
}
