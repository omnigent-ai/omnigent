import { useEffect, useState } from "react";
import { CheckIcon, ChevronDownIcon, PlusIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
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
 * Centered title-bar server picker for the macOS Electron shell.
 *
 * The shell hides the native title bar (titleBarStyle "hiddenInset"), so the
 * strip at the top of the window — normally the OS title — is blank canvas
 * owned by the web layer. This fills its center with "Omnigent — <host>" and
 * a chevron; clicking opens a menu of recently-connected servers (switching
 * re-points the whole window via the shell) plus "Connect to new server…",
 * which returns the window to the shell's setup page.
 *
 * When a thread is open, its title replaces the "Omnigent" brand label
 * (becoming "<title> — <host>") so the window title tracks what the user
 * is looking at, like a document window.
 *
 * Renders nothing until the shell confirms this page is a connected server
 * (getServerPicker resolves non-null) — so it's absent in plain browsers,
 * under shells too old for the picker IPC, and on foreign pages.
 */
export function TitleBarServerPicker({
  threadTitle,
}: {
  /** Title of the currently open thread, or null/undefined when no thread
      is selected or it has no title yet (falls back to "Omnigent"). */
  threadTitle?: string | null;
}) {
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

  return (
    /* Sits over the drag strip; the button itself is no-drag via the blanket
       [data-electron-mac] rule in index.css, so it stays clickable. */
    <div className="pointer-events-none absolute inset-x-0 top-0 z-40 flex h-9 justify-center">
      <DropdownMenu>
        <DropdownMenuTrigger
          className={cn(
            "pointer-events-auto flex max-w-72 items-center gap-1 rounded-md px-2 text-xs",
            "my-1 text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
            "data-[state=open]:bg-foreground/5 data-[state=open]:text-foreground",
          )}
          title="Switch server"
        >
          <span className="truncate font-medium">
            {threadTitle || "Omnigent"} — {hostOf(info.currentOrigin)}
          </span>
          <ChevronDownIcon className="size-3 shrink-0" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="center" className="min-w-56">
          <DropdownMenuItem disabled className="gap-2 opacity-100">
            <CheckIcon className="size-4 shrink-0" />
            <span className="truncate">{hostOf(info.currentOrigin)}</span>
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
