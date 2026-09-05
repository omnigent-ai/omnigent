import { AlertTriangleIcon, XIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/**
 * Warns that this tab is over the origin-wide stream budget: every stream slot
 * is held by other tabs (see `streamSlots`) and this tab had to open its
 * conversation over budget, so its live updates may lag until a tab is closed.
 *
 * Floats as a rounded card at the top-right, just below the chat header
 * (positioned like `JumpToTopButton`, so it must render as a sibling inside the
 * `relative` conversation container). Dismissable per over-budget episode — a
 * fresh episode re-shows it (see `streamBudgetBannerDismissed`).
 */
export function StreamBudgetBanner() {
  const exceeded = useChatStore((s) => s.streamBudgetExceeded);
  const dismissed = useChatStore((s) => s.streamBudgetBannerDismissed);
  const dismiss = useChatStore((s) => s.dismissStreamBudgetBanner);

  if (!exceeded || dismissed) return null;

  return (
    <div
      // Below the h-14 (56px) header; z-40 clears the z-30 header, matching
      // JumpToTopButton. The inset var (0px off the iOS shell) keeps it aligned
      // when the header shifts down by the safe-area inset. Right-aligned.
      style={{ top: "calc(56px + var(--omnigent-inset-top, 0px))" }}
      className="pointer-events-none absolute inset-x-0 z-40 flex justify-end px-3"
    >
      <div
        role="status"
        aria-live="polite"
        className={cn(
          "pointer-events-auto flex max-w-2xl items-center gap-2.5 rounded-xl px-3.5 py-2 text-ui shadow-lg",
          "border border-amber-300 bg-amber-100 text-amber-900",
          "dark:border-amber-700/60 dark:bg-amber-950 dark:text-amber-100",
          "animate-in fade-in-0 slide-in-from-top-1 duration-200",
        )}
      >
        <AlertTriangleIcon className="size-4 shrink-0" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          You have a lot of conversations open across your tabs. If you experience any slowness,
          please close a few tabs.
        </span>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss"
          className={cn(
            "-mr-1 shrink-0 rounded p-0.5 text-amber-900/60 hover:bg-amber-200 hover:text-amber-900",
            "dark:text-amber-100/60 dark:hover:bg-amber-900 dark:hover:text-amber-100",
          )}
        >
          <XIcon className="size-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
