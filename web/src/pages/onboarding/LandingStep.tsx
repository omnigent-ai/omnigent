// Onboarding step 1: the hero landing. Without MDM presets: "Get started
// locally" / "Join your team", or "Continue locally" above a "Join your team"
// split button once a local install exists. With presets, that split button is
// the only CTA. Its dropdown selects from presets, recents, and a server URL field.

import { Fragment, useEffect, useRef, useState } from "react";
import { ArrowRight, ChevronDown, Laptop, Plus, SquareArrowOutUpRight, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { isLocalInstall, normalizeServerUrl } from "@/pages/onboarding/ServerSelectStep";
import type { ConnectProgress } from "@/pages/onboarding/ServerSelectorV2";
import { ConnectStatus } from "@/pages/onboarding/primitives";
import { ownServerName } from "@/lib/serverNames";

/** Team name for a preset server URL: the host's first label. */
function teamName(url: string): string {
  const host = url.replace(/^https?:\/\//i, "").replace(/[/?#].*$/, "");
  return host.split(".")[0] || host;
}

function displayUrl(url: string): string {
  return url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
}

export function LandingStep({
  managedServers,
  managedServerNames,
  serverNames,
  recentServers,
  error,
  connection = null,
  onCancelConnect,
  onGetStarted,
  onContinueLocally,
  onCreateServer,
  onJoinServer,
  onJoinManaged,
  onJoinUrl,
}: {
  managedServers: string[];
  /** Display names for preset servers, server URL → name. */
  managedServerNames?: Record<string, string>;
  /** Names servers gave themselves, origin → name. */
  serverNames?: Record<string, string>;
  /** Recent non-preset servers, listed in the team dropdown. */
  recentServers: string[];
  /** Connect error to show above the CTAs (dropdown landings only). */
  error?: string;
  /** Progress of an in-flight join (dropdown landings only; null when idle). */
  connection?: ConnectProgress | null;
  onCancelConnect?: () => void;
  onGetStarted: () => void;
  /** Open the local install. Without presets, replaces "Get started locally"
   *  and turns "Join your team" into the dropdown of the other servers. */
  onContinueLocally?: () => void;
  /** Open the server deploy docs (offered beside "Continue locally"). */
  onCreateServer: () => void;
  onJoinServer: () => void;
  /** Join the selected preset server. */
  onJoinManaged: (url: string) => void;
  /** Join the selected recent or typed server URL. */
  onJoinUrl: (url: string) => void;
}) {
  // Close the dropdown during a join so it cannot change the selection or cover progress.
  const [menuOpen, setMenuOpen] = useState(false);
  // The menu spans the whole split button, not just its chevron trigger.
  const ctaRef = useRef<HTMLDivElement>(null);
  const [menuWidth, setMenuWidth] = useState<number>();
  const connecting = connection !== null;
  useEffect(() => {
    if (connecting) setMenuOpen(false);
  }, [connecting]);
  const hasPresets = managedServers.length > 0;
  const continueLocally = hasPresets ? undefined : onContinueLocally;
  const dropdownLanding = hasPresets || continueLocally !== undefined;
  // "Continue locally" covers the local install, so the dropdown lists the rest.
  const teamRecents = continueLocally
    ? recentServers.filter((url) => !isLocalInstall(url))
    : recentServers;
  // Which CTA started the in-flight connect, so only it shows the spinner.
  const [pendingCta, setPendingCta] = useState<"local" | "team">("team");
  const [selection, setSelection] = useState<string>();
  const selectedUrl: string | undefined = selection ?? managedServers[0] ?? teamRecents[0];
  const [typedUrl, setTypedUrl] = useState("");
  const [invalid, setInvalid] = useState(false);
  const openMenu = (open: boolean) => {
    if (open) setMenuWidth(ctaRef.current?.offsetWidth);
    setMenuOpen(open);
  };
  const selectTyped = () => {
    const url = normalizeServerUrl(typedUrl);
    if (url === null) setInvalid(true);
    else {
      setSelection(url);
      setMenuOpen(false);
    }
  };
  // With nothing to join yet, the main button opens the menu to pick or paste one.
  const joinSelected = () => {
    if (selectedUrl === undefined) {
      openMenu(true);
      return;
    }
    setPendingCta("team");
    if (managedServers.includes(selectedUrl)) onJoinManaged(selectedUrl);
    else onJoinUrl(selectedUrl);
  };
  const servers = [
    ...new Set([...managedServers, ...teamRecents, ...(selection ? [selection] : [])]),
  ];
  const firstRecent = servers.find((url) => !managedServers.includes(url));
  const nameOf = (url: string) =>
    managedServerNames && Object.hasOwn(managedServerNames, url) ? managedServerNames[url] : null;
  const listedName = (url: string) => {
    const own = ownServerName(serverNames, url);
    return nameOf(url) ?? (own ? `${own} (${displayUrl(url)})` : displayUrl(url));
  };

  // Primary when it's the only CTA; secondary beneath "Continue locally".
  const joinTeamDropdown = (
    <div ref={ctaRef} className="flex gap-0">
      <Button
        onClick={joinSelected}
        variant={continueLocally ? "outline" : "default"}
        loading={connecting && pendingCta === "team"}
        disabled={connecting}
        className={
          continueLocally
            ? "h-9 min-w-0 flex-1 rounded-r-none border-r-0"
            : "min-w-0 flex-1 py-5 rounded-tr-none rounded-br-none border-none"
        }
      >
        <Users className="size-4" />
        {selectedUrl === undefined ? (
          "Join your team"
        ) : (
          <span className="truncate" title={selectedUrl}>
            Join your team (
            <span className="opacity-80 font-normal">
              {managedServers.includes(selectedUrl)
                ? (nameOf(selectedUrl) ?? teamName(selectedUrl))
                : listedName(selectedUrl)}
            </span>
            )
          </span>
        )}
      </Button>
      {continueLocally && <span aria-hidden className="w-px self-stretch bg-border" />}
      <DropdownMenu open={menuOpen} onOpenChange={openMenu}>
        <DropdownMenuTrigger asChild>
          <Button
            variant={continueLocally ? "outline" : "default"}
            className={
              continueLocally
                ? "h-9 rounded-l-none border-l-0"
                : "py-5 rounded-tl-none rounded-bl-none border-0 border-l-[1px] border-muted-foreground"
            }
            aria-label="Choose team URL"
            disabled={connecting}
          >
            <ChevronDown className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" style={{ width: menuWidth }}>
          <DropdownMenuRadioGroup value={selectedUrl} onValueChange={setSelection}>
            {servers.map((url) => {
              const name =
                nameOf(url) ??
                ownServerName(serverNames, url) ??
                (managedServers.includes(url) ? teamName(url) : null);
              return (
                <Fragment key={url}>
                  {url === firstRecent && (
                    <DropdownMenuLabel className="text-muted-foreground">
                      Recent servers
                    </DropdownMenuLabel>
                  )}
                  <DropdownMenuRadioItem
                    value={url}
                    data-active={url === selectedUrl || undefined}
                    className="px-1.5 data-[active=true]:bg-muted data-[active=true]:font-medium [&>[data-slot=dropdown-menu-radio-item-indicator]]:hidden"
                  >
                    <Users className="size-4" aria-hidden />
                    <span className="truncate" title={url}>
                      {name ? (
                        <>
                          {name}{" "}
                          <span className="text-sm text-muted-foreground">
                            ({getHostnameFromUrl(url)})
                          </span>
                        </>
                      ) : (
                        url
                      )}
                    </span>
                  </DropdownMenuRadioItem>
                </Fragment>
              );
            })}
          </DropdownMenuRadioGroup>
          {servers.length > 0 && <DropdownMenuSeparator />}
          {/* Typed in place; keys stay in the field instead of driving the menu. */}
          <div className="flex items-center gap-1 p-1" onKeyDown={(e) => e.stopPropagation()}>
            <Input
              value={typedUrl}
              onChange={(e) => {
                setTypedUrl(e.target.value);
                setInvalid(false);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") selectTyped();
              }}
              placeholder="Enter Omnigent server URL"
              aria-label="Server URL"
              aria-invalid={invalid}
            />
            <Button
              size="icon"
              variant="ghost"
              disabled={typedUrl.trim() === ""}
              onClick={selectTyped}
              aria-label="Select server"
            >
              <ArrowRight className="size-4" />
            </Button>
          </div>
          {invalid && (
            <p role="alert" className="px-2 pb-1 text-sm text-destructive">
              Enter a valid http(s) server URL.
            </p>
          )}
          {continueLocally && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={onCreateServer} className="px-1.5">
                <Plus className="size-4" aria-hidden />
                Create a new server
                <SquareArrowOutUpRight
                  className="ml-auto size-4 text-muted-foreground"
                  aria-hidden
                />
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );

  return (
    <div className="flex min-w-0 flex-1 flex-col gap-2 px-2 pb-1">
      <div className="text-center flex-1 flex flex-col justify-center">
        <h1 className="text-2xl font-normal leading-9 tracking-[-0.03em] text-foreground">
          Meet Omnigent
        </h1>
        <p className="mt-1 text-base text-muted-foreground">
          One interface for all your coding agents
        </p>
      </div>

      {dropdownLanding && error && (
        <div role="alert" className="text-base text-destructive">
          <span className="font-medium">Couldn&apos;t connect to the server: </span>
          {error}
        </div>
      )}

      {hasPresets ? (
        // Only CTA: join the server selected in the dropdown.
        joinTeamDropdown
      ) : continueLocally ? (
        <>
          <Button
            onClick={() => {
              setPendingCta("local");
              continueLocally();
            }}
            loading={connecting && pendingCta === "local"}
            disabled={connecting}
            className="h-9"
          >
            <Laptop className="size-4" />
            Continue locally
          </Button>
          {joinTeamDropdown}
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

      <ConnectStatus connection={connection} onCancel={onCancelConnect} />
    </div>
  );
}

const getHostnameFromUrl = (url: string) => {
  const urlObject = new URL(url);
  return urlObject.hostname;
};
