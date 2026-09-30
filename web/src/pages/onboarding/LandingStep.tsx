// Onboarding step 1: the hero landing. Without MDM presets: "Get started
// locally" / "Join your team". With presets: one "Join your team (<name>)" split
// button; its dropdown lists other presets, recents, and a server URL field.

import { useState } from "react";
import { ArrowRight, ChevronDown, Laptop, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { normalizeServerUrl } from "@/pages/onboarding/ServerSelectStep";

/** Team name for a preset server URL: the host's first label, capitalized
 *  ("https://team.example.com/x" → "Team"). */
function teamName(url: string): string {
  const host = url.replace(/^https?:\/\//i, "").replace(/[/?#].*$/, "");
  const label = host.split(".")[0] || host;
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
}

export function LandingStep({
  managedServers,
  recentServers,
  error,
  onGetStarted,
  onJoinServer,
  onJoinManaged,
  onJoinUrl,
}: {
  managedServers: string[];
  /** Recent non-preset servers, listed in the preset dropdown. */
  recentServers: string[];
  /** Connect error to show above the CTA (MDM landing only). */
  error?: string;
  onGetStarted: () => void;
  onJoinServer: () => void;
  /** Join a preset server (the split button + its dropdown). */
  onJoinManaged: (url: string) => void;
  /** Join a recent or typed server URL (preset dropdown). */
  onJoinUrl: (url: string) => void;
}) {
  const hasPresets = managedServers.length > 0;
  const [typedUrl, setTypedUrl] = useState("");
  const [invalid, setInvalid] = useState(false);
  const joinTyped = () => {
    const url = normalizeServerUrl(typedUrl);
    if (url === null) setInvalid(true);
    else onJoinUrl(url);
  };
  const otherServers = [...managedServers.slice(1), ...recentServers];

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

      {hasPresets && error && (
        <div role="alert" className="text-base text-destructive">
          <span className="font-medium">Couldn&apos;t connect to the server: </span>
          {error}
        </div>
      )}

      {hasPresets ? (
        // Only CTA: join the first preset, or pick another / type a URL.
        <div className="flex gap-0">
          <Button
            onClick={() => onJoinManaged(managedServers[0])}
            className="flex-1 py-5 rounded-tr-none rounded-br-none border-none"
          >
            <Users className="size-4" />
            <span>
              Join your team (
              <span className="opacity-80 font-normal">{teamName(managedServers[0])}</span>)
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
            <DropdownMenuContent align="end" className="w-80">
              {otherServers.map((url) => (
                <DropdownMenuItem
                  key={url}
                  onSelect={() =>
                    managedServers.includes(url) ? onJoinManaged(url) : onJoinUrl(url)
                  }
                >
                  {displayUrl(url)}
                </DropdownMenuItem>
              ))}
              {otherServers.length > 0 && <DropdownMenuSeparator />}
              {/* Typed in place; keys stay in the field instead of driving the menu. */}
              <div className="flex items-center gap-1 p-1" onKeyDown={(e) => e.stopPropagation()}>
                <Input
                  value={typedUrl}
                  onChange={(e) => {
                    setTypedUrl(e.target.value);
                    setInvalid(false);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") joinTyped();
                  }}
                  placeholder="Enter Omnigent server URL"
                  aria-label="Server URL"
                  aria-invalid={invalid}
                />
                <Button
                  size="icon"
                  variant="ghost"
                  disabled={typedUrl.trim() === ""}
                  onClick={joinTyped}
                  aria-label="Join server"
                >
                  <ArrowRight className="size-4" />
                </Button>
              </div>
              {invalid && (
                <p role="alert" className="px-2 pb-1 text-sm text-destructive">
                  Enter a valid http(s) server URL.
                </p>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
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
