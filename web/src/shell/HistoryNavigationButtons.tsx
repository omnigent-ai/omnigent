import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { withBasePath } from "@/lib/basePath";
import {
  isAndroidShell,
  isIOSShell,
  isNativeShell,
  supportsNativeServerPicker,
} from "@/lib/nativeBridge";
import { useNavigate, useRebasePath } from "@/lib/routing";

/**
 * The Navigation API inside a native shell, or undefined in a browser tab
 * (which has its own Back/Forward) and in WebViews that predate the API.
 */
function shellNavigation(): Navigation | undefined {
  if (!isNativeShell()) return undefined;
  // Phone shells without the sidebar server picker float a switcher over the header.
  if ((isIOSShell() || isAndroidShell()) && !supportsNativeServerPicker()) return undefined;
  const navigation = (window as { navigation?: Navigation }).navigation;
  return typeof navigation?.entries === "function" ? navigation : undefined;
}

function subscribeToHistory(onChange: () => void): () => void {
  const navigation = shellNavigation();
  navigation?.addEventListener("currententrychange", onChange);
  return () => navigation?.removeEventListener("currententrychange", onChange);
}

/**
 * Whether the history entry `offset` steps away is a page of this app: same
 * origin and under `appRoot`. A workspace-hosted app shares its origin with
 * pages outside its mount, such as the workspace root the desktop shell
 * redirects from.
 */
function canStep(offset: -1 | 1, appRoot: string): boolean {
  const navigation = shellNavigation();
  const current = navigation?.currentEntry;
  if (!navigation || !current) return false;
  const target = navigation.entries()[current.index + offset]?.url;
  if (!target) return false;
  const url = new URL(target);
  if (url.origin !== window.location.origin) return false;
  return !appRoot || url.pathname === appRoot || url.pathname.startsWith(`${appRoot}/`);
}

/**
 * In-app Back / Forward for the desktop and mobile shells, which have no
 * browser toolbar. They step through this window's page history and stop at
 * the first entry outside the app.
 */
export function HistoryNavigationButtons() {
  const navigate = useNavigate();
  // The mount path: the server's base path, or the embedding host's basename.
  const appRoot = withBasePath(useRebasePath()("/")).replace(/\/+$/, "");
  const backEnabled = useSyncExternalStore(subscribeToHistory, () => canStep(-1, appRoot));
  const forwardEnabled = useSyncExternalStore(subscribeToHistory, () => canStep(1, appRoot));
  if (!shellNavigation()) return null;
  return (
    <div className="flex items-center gap-0.5" data-testid="history-navigation">
      <HistoryButton
        label="Back"
        componentId="chat.header.history_back"
        enabled={backEnabled}
        onClick={() => navigate(-1)}
      >
        <ChevronLeftIcon className="size-4 max-md:size-5" />
      </HistoryButton>
      <HistoryButton
        label="Forward"
        componentId="chat.header.history_forward"
        enabled={forwardEnabled}
        onClick={() => navigate(1)}
      >
        <ChevronRightIcon className="size-4 max-md:size-5" />
      </HistoryButton>
    </div>
  );
}

function HistoryButton({
  label,
  componentId,
  enabled,
  onClick,
  children,
}: {
  label: string;
  componentId: string;
  enabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
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
          disabled={!enabled}
          onClick={onClick}
          className="pointer-events-auto border-none text-muted-foreground hover:text-foreground max-md:size-11"
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}
