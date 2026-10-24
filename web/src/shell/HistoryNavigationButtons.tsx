import { ArrowLeftIcon, ArrowRightIcon, type LucideIcon } from "lucide-react";
import { useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsEmbedded } from "@/lib/embedded";

/**
 * Back and Forward for the sidebar header cluster (see SidebarHeaderActions),
 * the only ones the desktop shell's window has.
 *
 * Both go through the Navigation API, which sees only this window's run of
 * same-origin entries: an HTML preview iframe's entries don't count, unlike with
 * history.go(), and a cross-origin entry, such as the desktop setup page, ends
 * the run, so neither button can leave the app.
 */
export function HistoryNavigationButtons() {
  const isEmbedded = useIsEmbedded();
  const canGoBack = useSyncExternalStore(subscribe, () => window.navigation?.canGoBack === true);
  const canGoForward = useSyncExternalStore(
    subscribe,
    () => window.navigation?.canGoForward === true,
  );
  // An embedding host owns the page's history; older browsers lack the API.
  if (isEmbedded || !window.navigation) return null;

  return (
    // Like the cluster's Settings and toggle, hidden in the mobile drawer.
    <div className="history-navigation-buttons flex items-center gap-1 max-md:hidden">
      <HistoryButton
        label="Back"
        icon={ArrowLeftIcon}
        componentId="sidebar.back"
        available={canGoBack}
        onClick={() => settle(window.navigation.back())}
      />
      <HistoryButton
        label="Forward"
        icon={ArrowRightIcon}
        componentId="sidebar.forward"
        available={canGoForward}
        onClick={() => settle(window.navigation.forward())}
      />
    </div>
  );
}

function subscribe(onChange: () => void): () => void {
  const navigation = window.navigation;
  navigation?.addEventListener("currententrychange", onChange);
  return () => navigation?.removeEventListener("currententrychange", onChange);
}

// A repeat click before a step lands targets the same entry, so the API merges
// it into that step. A refused or interrupted step rejects both promises and
// leaves the app where it was; settling them keeps the rejection handled.
function settle({ committed, finished }: NavigationResult): void {
  void Promise.allSettled([committed, finished]);
}

function HistoryButton({
  label,
  icon: Icon,
  componentId,
  available,
  onClick,
}: {
  label: string;
  icon: LucideIcon;
  componentId: string;
  available: boolean;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          componentId={componentId}
          // Greyed with aria-disabled, not disabled, so a focused button keeps
          // keyboard focus when navigation reaches either end of the history.
          aria-disabled={!available || undefined}
          onClick={available ? onClick : undefined}
          className="text-muted-foreground hover:text-foreground aria-disabled:pointer-events-none aria-disabled:opacity-50"
        >
          <Icon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}
