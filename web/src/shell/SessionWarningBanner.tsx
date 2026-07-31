// Session-scoped warning strip floating under the chat header.
//
// Surfaces degraded-but-running conditions the user must know about while
// the session keeps working — today only `subagent_routing_unenforced`,
// published when a harness's router hook never fired, so sub-agent model
// routing is advisory rather than enforced for that harness.

import { AlertTriangleIcon } from "lucide-react";
import type { SessionWarning } from "@/lib/types";

/** Warning code for "a harness ran without the router hook enforcing picks". */
export const SUBAGENT_ROUTING_UNENFORCED = "subagent_routing_unenforced";

/**
 * Copy per warning code — this map IS the set of codes the banner renders,
 * so a new code cannot be listed without copy for it (and an unknown code is
 * ignored rather than leaking a machine string into the header).
 *
 * A `Map`, not an object literal: codes arrive from the wire, and a plain
 * record would answer to `Object.prototype` keys — `"__proto__"` /
 * `"toString"` would pass the filter and then throw (or render garbage)
 * while building the header.
 *
 * @param harness - Harness the warning names, when it names one.
 */
const WARNING_TITLES = new Map<string, (harness: string | undefined) => string>([
  [
    SUBAGENT_ROUTING_UNENFORCED,
    (harness) =>
      harness
        ? `Sub-agent routing isn't enforced on ${harness}`
        : "Sub-agent routing isn't enforced",
  ],
]);

/**
 * Session warnings this banner knows how to render, in wire order.
 *
 * @param warnings - The snapshot's warnings, e.g. from `Session.warnings`.
 * @returns Only the renderable entries; empty when there is nothing to show.
 */
export function renderableWarnings(
  warnings: SessionWarning[] | null | undefined,
): SessionWarning[] {
  return (warnings ?? []).filter((warning) => WARNING_TITLES.has(warning.code));
}

/** Headline for one warning, keyed off its code; empty for an unknown code. */
function warningTitle(warning: SessionWarning): string {
  const title = WARNING_TITLES.get(warning.code);
  if (title === undefined) return "";
  const harness = warning.harness?.trim();
  return title(harness === "" ? undefined : harness);
}

/**
 * Warning strip for the active session. Renders nothing when the session
 * has no warning the UI knows about — the common case.
 *
 * Floats over the top of the chat column instead of sitting in the flow, so
 * appearing mid-session never shifts the conversation down. It shares the
 * chat header's positioning contract: anchored inside the chat column (never
 * over the sidebar), stopping short of the workspace panel via
 * `--workspace-panel-offset`, and stacked just under the header's z-30. The
 * strip itself ignores pointer events so the chat underneath stays
 * scrollable and clickable; each warning row takes them back.
 *
 * @param warnings - The session snapshot's warnings.
 */
export function SessionWarningBanner({ warnings }: { warnings?: SessionWarning[] | null }) {
  const visible = renderableWarnings(warnings);
  if (visible.length === 0) return null;
  return (
    <div
      data-testid="session-warning-banner"
      className="pointer-events-none absolute inset-x-0 top-14 z-20 flex flex-col items-start gap-1 px-2 md:right-[var(--workspace-panel-offset,0px)]"
    >
      {visible.map((warning) => (
        <div
          key={`${warning.code}:${warning.harness ?? ""}`}
          data-testid={`session-warning-${warning.code}`}
          className="pointer-events-auto flex max-w-2xl items-start gap-2 rounded-md border border-border bg-warning/10 px-3 py-1.5 text-xs text-foreground shadow-md backdrop-blur-xl backdrop-saturate-150"
        >
          <AlertTriangleIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <span className="min-w-0">
            <span className="font-medium">{warningTitle(warning)}</span>
            {warning.reason ? (
              <span className="text-muted-foreground"> · {warning.reason}</span>
            ) : null}
          </span>
        </div>
      ))}
    </div>
  );
}
