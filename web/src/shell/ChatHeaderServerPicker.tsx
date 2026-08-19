import { type FormEvent, useEffect, useState } from "react";
import { CheckIcon, ChevronDownIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import {
  connectWebServer,
  getServerPicker,
  openServerSetup,
  switchServer,
  type UnifiedServerPickerInfo,
} from "@/lib/serverPicker";

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

function isCurrentServer(url: string, current: string): boolean {
  const currentOrigin = originOf(current);
  return currentOrigin ? originOf(url) === currentOrigin : url === current;
}

/** Server switcher shown beside the centered conversation title. */
export function ChatHeaderServerPicker({ showBrand = false }: { showBrand?: boolean }) {
  const [info, setInfo] = useState<UnifiedServerPickerInfo | null>(null);
  const [connectOpen, setConnectOpen] = useState(false);
  const [serverUrl, setServerUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

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

  const others = info.recentServers.filter((url) => !isCurrentServer(url, info.currentOrigin));
  const currentHost = hostOf(info.currentOrigin);

  function onConnectSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      connectWebServer(serverUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not connect to that server.");
    }
  }

  return (
    <>
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
            <DropdownMenuItem
              key={url}
              className="gap-2"
              onSelect={() => void switchServer(url, info.runtime)}
            >
              <span className="size-4 shrink-0" aria-hidden="true" />
              <span className="truncate">{hostOf(url)}</span>
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="gap-2"
            onSelect={() => {
              if (info.runtime === "desktop") {
                openServerSetup();
              } else {
                setServerUrl("");
                setError(null);
                setConnectOpen(true);
              }
            }}
          >
            <PlusIcon className="size-4 shrink-0" />
            Connect to new server…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={connectOpen} onOpenChange={setConnectOpen}>
        <DialogContent className="sm:max-w-md">
          <form className="contents" onSubmit={onConnectSubmit}>
            <DialogHeader>
              <DialogTitle>Connect to server</DialogTitle>
              <DialogDescription>
                Enter an Omnigent server URL. Loopback addresses default to HTTP; other hosts
                default to HTTPS.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <label htmlFor="connect-server-url" className="text-ui font-medium">
                Server URL
              </label>
              <Input
                id="connect-server-url"
                autoFocus
                value={serverUrl}
                onChange={(event) => setServerUrl(event.target.value)}
                placeholder="localhost:6767"
                aria-invalid={Boolean(error)}
              />
              {error && <p className="text-sm text-destructive">{error}</p>}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setConnectOpen(false)}>
                Cancel
              </Button>
              <Button type="submit">Connect</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
