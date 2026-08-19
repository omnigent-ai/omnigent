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

/** Electron server switcher shown beside the centered conversation title. */
export function ChatHeaderServerPicker({ showBrand = false }: { showBrand?: boolean }) {
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

  const others = info.recentServers.filter((url) => originOf(url) !== info.currentOrigin);
  const currentHost = hostOf(info.currentOrigin);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex min-w-0 items-center gap-1 rounded-md px-1 text-ui text-muted-foreground hover:bg-foreground/5 hover:text-foreground data-[state=open]:bg-foreground/5 data-[state=open]:text-foreground"
        aria-label={`Server: ${currentHost}. Switch server`}
        data-testid="chat-header-server-picker"
        title="Switch server"
      >
        {showBrand && <span className="truncate font-medium">Omnigent</span>}
        <span aria-hidden className="shrink-0 opacity-40">
          —
        </span>
        <span className="truncate font-medium">{currentHost}</span>
        <ChevronDownIcon className="size-3 shrink-0" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="center" className="min-w-56">
        <DropdownMenuItem disabled className="gap-2 opacity-100">
          <CheckIcon className="size-4 shrink-0" />
          <span className="truncate font-medium">{currentHost}</span>
        </DropdownMenuItem>
        {others.map((url) => (
          <DropdownMenuItem key={url} className="gap-2" onSelect={() => void switchServer(url)}>
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
  );
}
