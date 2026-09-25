// Onboarding step 1: the hero landing. Without MDM presets: "Get started
// locally" opens deployment-mode select and "Join your team" opens server
// select. With MDM presets: "Join your team (<name>)" becomes a primary split
// button (button + dropdown of preset servers) and "Get started locally" drops
// to secondary. Rendered inside the card body below the animated panel.

import { ChevronDown, Laptop, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** Short label for a preset server URL — host without scheme / trailing slash,
 *  first label of the host (mirrors the mock's "field-eng-omni"). */
function shortName(url: string): string {
  const host = url.replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
  return host.split(".")[0] || host;
}

export function LandingStep({
  managedServers,
  onGetStarted,
  onJoinServer,
  onJoinManaged,
}: {
  managedServers: string[];
  onGetStarted: () => void;
  onJoinServer: () => void;
  /** Join a specific preset server (the split button + its dropdown). */
  onJoinManaged: (url: string) => void;
}) {
  const hasPresets = managedServers.length > 0;

  return (
    <div className="flex flex-1 flex-col gap-2 px-2 pb-1">
      <div className="text-center flex-1 flex flex-col justify-center">
        <h1 className="text-2xl font-normal leading-9 tracking-[-0.03em] text-foreground">
          Meet Omnigent
        </h1>
        <p className="mt-1 text-base text-muted-foreground">
          One interface for all your coding agents
        </p>
      </div>

      {hasPresets ? (
        <>
          {/* Primary split button: join the first preset, or pick another. */}
          <div className="flex gap-0">
            <Button
              onClick={() => onJoinManaged(managedServers[0])}
              className="flex-1 py-5 rounded-tr-none rounded-br-none border-none"
            >
              <span>
                Join your team (
                <span className="opacity-80 font-normal">{shortName(managedServers[0])}</span>)
              </span>
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  className="py-5 rounded-tl-none rounded-bl-none border-0 border-l-[1px] border-muted-foreground"
                  aria-label="Choose team URL"
                >
                  <ChevronDown className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {managedServers.map((url) => (
                  <DropdownMenuItem key={url} onSelect={() => onJoinManaged(url)}>
                    {url.replace(/^https?:\/\//i, "").replace(/\/$/, "")}
                  </DropdownMenuItem>
                ))}
                {/* Escape hatch to the full list (presets + recents), which is
                    otherwise unreachable from the MDM landing. */}
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={onJoinServer}>Show all servers…</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
          <Button variant="outline" onClick={onGetStarted} className="py-5">
            Get started locally
          </Button>
        </>
      ) : (
        <>
          <Button onClick={onGetStarted} className="h-9">
            <Laptop className="size-4" />
            Get started locally
          </Button>
          <Button variant="outline" onClick={onJoinServer} className="h-9">
            <Users className="size-4" />
            Join your team
          </Button>
        </>
      )}
    </div>
  );
}
