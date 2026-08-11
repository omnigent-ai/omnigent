import { useEffect, useState } from "react";
import { CheckIcon, ChevronUpIcon, PlusIcon, ServerIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  getServerPicker,
  openServerSetup,
  switchServer,
  type ServerPickerInfo,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { SIDEBAR_ROW } from "./sidebarStyles";

/** Short display label for a server URL — its host, e.g. "localhost:8000". */
function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

/** Origin of a server URL, for matching recents against the current origin. */
function originOf(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

/**
 * Server picker for the Electron desktop shell, pinned to the sidebar's bottom.
 *
 * A sidebar row (server glyph + current host + an upward chevron) that opens a
 * menu of recently-connected servers — selecting one re-points the whole window
 * via the shell — plus "Connect to new server…", which returns the window to
 * the shell's setup page.
 *
 * This deliberately lives at the bottom of the sidebar rather than in the
 * window's title bar. The macOS shell hides the native title bar (titleBarStyle
 * "hiddenInset"), and the previous picker filled that freed strip with a
 * centered "<thread> — <host>" label. But the chat header occupies the same
 * strip (`absolute top-0`, and taller at h-14), so on a narrow window the
 * centered label ran into the header's action cluster. Docking the picker here
 * takes it out of that contested space; the drag strip and the sidebar's
 * traffic-light top margin stay exactly as they were, since those are what keep
 * the OS window controls off the sidebar card.
 *
 * Renders nothing until the shell confirms this page is a connected server
 * (getServerPicker resolves non-null) — so it's absent in plain browsers, under
 * shells too old for the picker IPC, and on foreign pages. That single check is
 * the whole gate: no platform sniffing, matching how the rest of nativeBridge
 * degrades (one bundle, many runtimes, decided at runtime). Note this reaches
 * every Electron platform, where the old title-bar picker was macOS-only —
 * Windows and Linux desktop users previously had no in-app picker at all.
 */
export function SidebarServerPicker() {
  const [info, setInfo] = useState<ServerPickerInfo | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getServerPicker().then((result) => {
      if (!cancelled) setInfo(result);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!info) return null;

  // The current server leads the list even when the recents file was edited
  // out from under us; recents matching the current origin collapse into it.
  const others = info.recentServers.filter((url) => originOf(url) !== info.currentOrigin);
  const currentHost = hostOf(info.currentOrigin);

  return (
    // shrink-0 keeps the row at its natural height so the scrolling session
    // list above (flex-1) gives up space instead of squashing it.
    <div className="shrink-0 px-2 pt-1 pb-2" data-testid="sidebar-server-picker-row">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            // Same shared row construct as New session / Inbox / Settings, so
            // the icon lands on the sidebar's icon column and the label on its
            // label column.
            className={cn(
              SIDEBAR_ROW,
              "w-full justify-start border-0 font-normal",
              "text-muted-foreground",
              "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
              "data-[state=open]:bg-muted data-[state=open]:text-foreground",
            )}
            aria-label={`Server: ${currentHost}. Switch server`}
            data-testid="sidebar-server-picker"
          >
            <ServerIcon className="ui-icon text-muted-foreground" />
            <span className="truncate">{currentHost}</span>
            {/* Points up: the menu opens upward from the sidebar's bottom. */}
            <ChevronUpIcon className="ui-icon ml-auto shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        {/* side="top" — the trigger sits at the bottom of the window, so the
            menu must grow upward rather than off-screen. */}
        <DropdownMenuContent side="top" align="start" className="min-w-56">
          <DropdownMenuLabel className="text-muted-foreground">Recents</DropdownMenuLabel>
          <DropdownMenuItem disabled className="gap-2 opacity-100">
            <CheckIcon className="size-4 shrink-0" />
            <span className="truncate font-medium">{currentHost}</span>
          </DropdownMenuItem>
          {others.map((url) => (
            <DropdownMenuItem key={url} className="gap-2" onSelect={() => void switchServer(url)}>
              {/* Spacer aligns hosts under the current-server check. */}
              <span className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{hostOf(url)}</span>
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem className="gap-2" onSelect={() => openServerSetup()}>
            <PlusIcon className="size-4 shrink-0" />
            Connect to new server…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
