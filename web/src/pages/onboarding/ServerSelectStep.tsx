// Onboarding step 3: join an existing server or add a new one. Lists
// recent/managed servers from the omnigentSetup bridge as selectable cards
// (each with a three-dot menu: view info / delete), plus a URL input to add
// one. Join connects to the chosen/entered server.

import { type ComponentType, useEffect, useState } from "react";
import {
  Cloudy,
  Copy,
  Info,
  Laptop,
  MoreHorizontal,
  Play,
  Plus,
  Server,
  SquareArrowOutUpRight,
  TabletSmartphone,
  Trash2,
  Users,
  type LucideProps,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import type { ConnectResult } from "@/pages/onboarding/ServerSelectorV2";
import { cn } from "@/lib/utils";

const DEFAULT_LOCAL = "http://localhost:6767";
const CREATE_SERVER_URL = "https://omnigent.ai/";
// A URL pointing at the loopback interface is a local install.
const LOCAL_HOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i;

// Hero tiles above the empty-state form — what a cloud/shared server offers.
const JOIN_HERO_ICONS: ComponentType<LucideProps>[] = [Cloudy, TabletSmartphone, Users, Play];

// Shown when there are no saved servers yet — what joining a server unlocks.
const JOIN_BENEFITS: { label: string; icon: ComponentType<LucideProps> }[] = [
  { label: "Access agents from any device", icon: TabletSmartphone },
  { label: "Co-drive live sessions with teammates", icon: Users },
  {
    label: "Keep sessions running in the cloud (requires separate Sandbox installation setup)",
    icon: Play,
  },
];

/** Strip the scheme for display, matching the shell's setup page. */
function displayName(url: string): string {
  return url.replace(/^https?:\/\//i, "");
}

function isLocal(url: string): boolean {
  return LOCAL_HOST_RE.test(url);
}

/** Card title: local servers read as "Local installation (host)". */
function serverTitle(url: string): string {
  return isLocal(url) ? `Local installation (${displayName(url)})` : displayName(url);
}

/**
 * Normalize a typed server URL to an absolute http(s) origin, or null if it
 * isn't a usable URL. A bare host ("localhost:6767", "example.com") gets an
 * http:// scheme; a non-http scheme (javascript:, file:) or garbage is
 * rejected — the shell still probes reachability on navigate, this just stops
 * obviously-wrong input from being connected. Mirrors electron/src/url.js;
 * the main process re-normalizes on connect, so this is only a client-side
 * pre-filter. Exported for its test (ServerSelectStep.test.ts).
 */
export function normalizeServerUrl(raw: string): string | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  // A scheme is anything before "://". If present it must be http(s); if absent
  // (bare host), default to http. This rejects file:, javascript:, ftp: etc.,
  // and "file:///x" (scheme "file") rather than mangling it into a fake host.
  const hasScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed);
  if (hasScheme && !/^https?:\/\//i.test(trimmed)) return null;
  const withScheme = hasScheme ? trimmed : `http://${trimmed}`;
  try {
    const url = new URL(withScheme);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (url.hostname === "") return null;
    return url.toString();
  } catch {
    return null;
  }
}

export function ServerSelectStep({
  initialUrl,
  error,
  recentServers,
  managedServers,
  onBack,
  onConnect,
  onRemove,
  onCopy,
}: {
  initialUrl: string;
  error?: string;
  recentServers: string[];
  managedServers: string[];
  onBack: () => void;
  /** Connect to a URL; resolves `{needsConfirm}` (call again with force) or
   *  `{error}` to show, else navigation is underway. */
  onConnect: (url: string, force?: boolean) => Promise<ConnectResult>;
  /** Remove a recent server from the list, if the shell supports it. */
  onRemove?: (url: string) => void;
  /** Copy text to the clipboard (native shell bridge — file:// blocks navigator.clipboard). */
  onCopy: (text: string) => void;
}) {
  // Selection: a listed server, or null when the user is typing a new URL.
  const listed = [...managedServers, ...recentServers];
  const [selected, setSelected] = useState<string | null>(listed[0] ?? null);
  const [typedUrl, setTypedUrl] = useState(
    initialUrl && initialUrl !== DEFAULT_LOCAL ? initialUrl : "",
  );
  const [invalid, setInvalid] = useState(false);
  // The URL the shell flagged as "doesn't look like Omnigent" — a second
  // connect on the same URL proceeds (force); editing the input clears it.
  const [unconfirmedUrl, setUnconfirmedUrl] = useState<string | null>(null);
  // Message from a rejected connect (e.g. main-side normalizeUrl rejected an
  // input the renderer accepted), so a failed Join shows something.
  const [connectError, setConnectError] = useState<string | null>(null);
  // The server whose info dialog is open, or null.
  const [infoUrl, setInfoUrl] = useState<string | null>(null);

  // If the server lists arrive after mount, default-select the first one.
  const firstListed = managedServers[0] ?? recentServers[0] ?? null;
  useEffect(() => {
    if (firstListed !== null) setSelected((prev) => prev ?? firstListed);
  }, [firstListed]);

  const typedNormalized = normalizeServerUrl(typedUrl);
  // Connect target: a selected list entry (already normalized), else the typed
  // URL if it parses. null when neither is usable.
  const chosen = selected ?? typedNormalized;

  const connect = async (url: string | null) => {
    if (url === null) {
      setInvalid(true);
      return;
    }
    setConnectError(null);
    // A second click on the already-warned URL forces through.
    const force = unconfirmedUrl === url;
    const result = await onConnect(url, force);
    setUnconfirmedUrl(result.needsConfirm ? url : null);
    setConnectError(result.error ?? null);
  };

  const removeServer = (url: string) => {
    onRemove?.(url);
    if (selected === url) setSelected(null);
  };

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <h1 className="mb-3 pt-1 text-center text-base text-foreground">
        Join an existing server or add a new one
      </h1>

      {(error || invalid || unconfirmedUrl || connectError) && (
        <div role="alert" className="mb-2 text-base text-destructive">
          {invalid
            ? "Enter a valid http(s) server URL."
            : connectError
              ? connectError
              : unconfirmedUrl
                ? "This doesn't look like an Omnigent server. Click Join again to connect anyway."
                : error}
        </div>
      )}

      {listed.length === 0 && (
        <div className="mb-4 flex justify-center gap-1.5" aria-hidden="true">
          {JOIN_HERO_ICONS.map((Icon, index) => (
            <span
              key={Icon.displayName ?? index}
              className={cn(
                "flex size-11 items-center justify-center rounded-2xl border bg-background",
                index === 0
                  ? "border-brand-accent/25 text-brand-accent"
                  : "border-border text-muted-foreground",
              )}
            >
              <Icon className="size-5" />
            </span>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2 rounded-lg border border-border px-3 py-1.5">
        <Input
          value={typedUrl}
          onChange={(e) => {
            setTypedUrl(e.target.value);
            setSelected(null);
            setInvalid(false);
            setUnconfirmedUrl(null);
            setConnectError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") connect(typedNormalized);
          }}
          placeholder="Enter Omnigent server URL"
          className="border-0 px-0 shadow-none focus-visible:ring-0"
          aria-label="Server URL"
        />
        <button
          type="button"
          disabled={typedUrl.trim().length === 0}
          onClick={() => connect(typedNormalized)}
          className="flex shrink-0 items-center gap-1 text-base text-muted-foreground disabled:opacity-50"
        >
          <Plus className="size-4" aria-hidden />
          Add
        </button>
      </div>

      {listed.length === 0 ? (
        // No saved servers yet: sell what joining unlocks + a way to spin one up.
        <div className="mt-4 flex min-h-0 flex-1 flex-col gap-3">
          {JOIN_BENEFITS.map((benefit) => (
            <span key={benefit.label} className="flex gap-2 text-base text-foreground">
              <benefit.icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
              <span>{benefit.label}</span>
            </span>
          ))}
          <a
            href={CREATE_SERVER_URL}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-2 text-base text-foreground underline underline-offset-2"
          >
            <Plus className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            Create your own Omnigent server
            <SquareArrowOutUpRight className="size-3.5 shrink-0" aria-hidden />
          </a>
        </div>
      ) : (
        <div className="mt-2 flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
          {listed.map((url, index) => {
            const isSelected = selected === url;
            // Managed (org-provided) servers can't be removed by the user.
            const removable = onRemove != null && recentServers.includes(url);
            return (
              <div
                key={url}
                className={cn(
                  "flex items-center gap-3 rounded-lg border-2 px-3 py-2.5 transition-[border-color,background-color]",
                  isSelected ? "border-primary bg-primary/5" : "border-border hover:bg-muted",
                )}
              >
                <button
                  type="button"
                  onClick={() => {
                    setSelected(url);
                    setTypedUrl("");
                    setInvalid(false);
                    setUnconfirmedUrl(null);
                    setConnectError(null);
                  }}
                  className="flex min-w-0 flex-1 items-center gap-3 text-left"
                  aria-pressed={isSelected}
                >
                  <span
                    aria-hidden
                    className={cn(
                      "flex size-4 shrink-0 items-center justify-center rounded-full border",
                      isSelected ? "border-primary" : "border-border",
                    )}
                  >
                    {isSelected && <span className="size-2 rounded-full bg-primary" />}
                  </span>
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-tag-pink text-brand-accent">
                    {isLocal(url) ? (
                      <Laptop className="size-4" aria-hidden />
                    ) : (
                      <Server className="size-4" aria-hidden />
                    )}
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate text-base text-foreground">{serverTitle(url)}</span>
                    <span className="flex items-center gap-1.5 text-base text-muted-foreground">
                      {index === 0 && (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] leading-none">
                          Last used
                        </span>
                      )}
                      <span className="truncate">{isLocal(url) ? "Local" : "Remote"}</span>
                    </span>
                  </span>
                </button>

                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      type="button"
                      className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                      aria-label={`More options for ${displayName(url)}`}
                    >
                      <MoreHorizontal className="size-4" aria-hidden />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onSelect={() => setInfoUrl(url)}>
                      <Info className="size-4" aria-hidden />
                      View server info
                    </DropdownMenuItem>
                    {removable && (
                      <DropdownMenuItem variant="destructive" onSelect={() => removeServer(url)}>
                        <Trash2 className="size-4" aria-hidden />
                        Delete from list
                      </DropdownMenuItem>
                    )}
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-3 flex gap-2">
        <Button variant="outline" className="flex-1" onClick={onBack}>
          Back
        </Button>
        <Button className="flex-1" disabled={chosen === null} onClick={() => connect(chosen)}>
          Join
        </Button>
      </div>

      <ServerInfoDialog url={infoUrl} onClose={() => setInfoUrl(null)} onCopy={onCopy} />
    </div>
  );
}

/**
 * "Omnigent server info" dialog. Shows only what the shell actually knows about
 * a saved server — its URL and whether it's local. The design mock also lists
 * admin / participants / last-used, but the setup bridge exposes none of that,
 * so we don't fabricate it.
 */
function ServerInfoDialog({
  url,
  onClose,
  onCopy,
}: {
  url: string | null;
  onClose: () => void;
  onCopy: (text: string) => void;
}) {
  return (
    <Dialog open={url !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-[420px]">
        <DialogHeader>
          <DialogTitle>Omnigent server info</DialogTitle>
        </DialogHeader>
        {url !== null && (
          <div className="flex flex-col gap-4 overflow-hidden">
            <span className="flex size-16 items-center justify-center rounded-2xl bg-tag-pink text-brand-accent">
              {isLocal(url) ? (
                <Laptop className="size-8" aria-hidden />
              ) : (
                <Server className="size-8" aria-hidden />
              )}
            </span>
            <div className="flex items-center justify-between gap-4">
              <span className="min-w-0 truncate text-base text-foreground">{displayName(url)}</span>
              <button
                type="button"
                onClick={() => onCopy(url)}
                className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                aria-label="Copy server URL"
              >
                <Copy className="size-4" aria-hidden />
              </button>
            </div>
            <div className="flex items-center justify-between border-t border-border pt-3 text-base">
              <span className="text-muted-foreground">Type</span>
              <span className="text-foreground">{isLocal(url) ? "Local" : "Remote"}</span>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
