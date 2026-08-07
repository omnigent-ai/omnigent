import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useStreamTabCount } from "@/hooks/useStreamTabCount";
import { connectionHasLowStreamLimit } from "@/lib/streamTabRegistry";
import { cn } from "@/lib/utils";

/**
 * Tabs-with-an-open-conversation at which we warn.
 *
 * Browsers allow ~6 concurrent HTTP/1.1 connections per origin, shared across
 * every tab in the profile, and each open conversation holds one for its live
 * event stream. At 6 the pool is full and unrelated requests — navigation, API
 * calls — queue behind the streams, which presents as the whole app hanging.
 * Warn at 5 so the message arrives while the app still works, rather than
 * appearing (or failing to load) once things are already wedged.
 */
const WARN_AT_TABS = 5;

/**
 * Warns when enough tabs hold a conversation stream to exhaust the browser's
 * per-origin connection pool.
 *
 * Advisory only — it explains a stall the app cannot otherwise account for and
 * names the one remedy available to the user (close a tab). It does not prevent
 * the exhaustion; removing the limit needs a transport that doesn't consume an
 * HTTP connection per conversation.
 *
 * Renders nothing when the page was served over HTTP/2/3 (multiplexed, so the
 * cap doesn't bind) or where Web Locks is unavailable (count reads 0).
 */
export function StreamTabLimitBanner() {
  const tabCount = useStreamTabCount();
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);

  // Re-arm after dismissal only if the situation gets WORSE. Dismissing at 5
  // shouldn't re-nag at 5, but crossing to 6 is new information.
  const suppressed = dismissedAt !== null && tabCount <= dismissedAt;

  if (tabCount < WARN_AT_TABS || suppressed || !connectionHasLowStreamLimit()) {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed inset-x-0 top-0 z-[100] flex flex-wrap items-center justify-center gap-x-3 gap-y-1 px-4 py-2",
        "border-b border-border bg-background/95 backdrop-blur",
        "supports-[backdrop-filter]:bg-background/80",
      )}
    >
      <span className="text-ui text-foreground">
        {tabCount} tabs have a conversation open. Browsers limit how many live connections one site
        may hold, so opening more can make Omnigent slow to respond — closing a few tabs restores
        it.
      </span>
      <Button size="sm" variant="ghost" onClick={() => setDismissedAt(tabCount)}>
        Dismiss
      </Button>
    </div>
  );
}
