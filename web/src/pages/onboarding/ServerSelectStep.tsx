// Onboarding step 3: join an existing server or add a new one. Lists
// recent/managed servers from the omnigentSetup bridge as selectable cards
// (each with a three-dot menu: view info / delete), plus a URL input to add
// one. Join connects to the chosen/entered server.

import { type ComponentType, type ReactNode, useEffect, useState } from "react";
import {
  ArrowLeft,
  Building,
  ChevronDown,
  Cloudy,
  Copy,
  Laptop,
  type LucideProps,
  MoreHorizontal,
  Play,
  Plus,
  Server,
  ShieldUser,
  SquareArrowOutUpRight,
  TabletSmartphone,
  Timer,
  Trash2,
  Users,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import type { ConnectResult, ServerCheckResult } from "@/pages/onboarding/ServerSelectorV2";
import { installActionLabel, OnboardingHeading } from "@/pages/onboarding/primitives";
import { cn } from "@/lib/utils";

const DEFAULT_LOCAL = "http://localhost:6767";
const CREATE_SERVER_URL = "https://omnigent.ai/";
// A URL pointing at the loopback interface is a local install.
const LOCAL_HOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i;

/** Strip the scheme for display, matching the shell's setup page. */
function displayName(url: string): string {
  // Strip the scheme and a bare trailing slash (origins normalize to
  // "http://host/" — the slash is noise in the label).
  return url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
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

/** Advisory reachability status for a just-added server. */
type CheckStatus = "checking" | "ok" | "reachable" | "unreachable";

function SectionHeader({ label, action }: { label: string; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-base font-medium text-muted-foreground">{label}</span>
      {action}
    </div>
  );
}

// Hero tiles — what a cloud/shared server offers. Shown in the panel band on the
// add-server / server-detail steps.
const HERO_ICONS: ComponentType<LucideProps>[] = [Cloudy, TabletSmartphone, Users, Play];

/** Overlapping hero-icon row for the panel band (add-server / detail steps). */
export function ServerHeroIcons() {
  return (
    <div className="flex -space-x-2" aria-hidden="true">
      {HERO_ICONS.map((Icon, index) => (
        <span
          key={Icon.displayName ?? index}
          className={cn(
            "flex size-12 items-center justify-center rounded-xl border bg-background",
            index === 0
              ? "border-brand-accent/25 text-brand-accent"
              : "border-border text-muted-foreground",
          )}
        >
          <Icon className="size-5" />
        </span>
      ))}
    </div>
  );
}

export function ServerSelectStep({
  initialUrl,
  error,
  recentServers,
  managedServers,
  installed,
  onBack,
  onConnect,
  onRemove,
  onCopy,
  onCheckServer,
  onAddModeChange,
}: {
  initialUrl: string;
  error?: string;
  recentServers: string[];
  managedServers: string[];
  /** Returning user (CLI installed) → "Open Omnigent"; new → "Install Omnigent". */
  installed?: boolean;
  /** Reports whether the URL-input ("add") view is showing, so the parent can
   *  swap the panel band (hero icons) for it. */
  onAddModeChange?: (addMode: boolean) => void;
  onBack: () => void;
  /** Connect to a URL; resolves `{needsConfirm}` (call again with force) or
   *  `{error}` to show, else navigation is underway. */
  onConnect: (url: string, force?: boolean) => Promise<ConnectResult>;
  /** Remove a recent server from the list, if the shell supports it. */
  onRemove?: (url: string) => void;
  /** Copy text to the clipboard (native shell bridge — file:// blocks navigator.clipboard). */
  onCopy: (text: string) => void;
  /** Advisory reachability probe for a just-added server. */
  onCheckServer: (url: string) => Promise<ServerCheckResult>;
}) {
  // Servers the user added this session (prepended to the persisted recents;
  // they only become real recents once actually connected to).
  const [addedServers, setAddedServers] = useState<string[]>([]);
  const listed = [...addedServers, ...managedServers, ...recentServers];

  // Exclusive focus: EITHER a listed server is selected (Join connects to it)
  // OR the input is active (Add adds a server; Join is disabled). Focusing the
  // input deselects; selecting a server clears the input.
  const [selected, setSelected] = useState<string | null>(null);
  // Pre-fill the input with the just-failed URL (retry after a bad connect
  // reloads with ?url=), else empty. The default localhost is a listed recent,
  // not something to type.
  const [typedUrl, setTypedUrl] = useState(
    error && initialUrl && initialUrl !== DEFAULT_LOCAL ? initialUrl : "",
  );
  const [invalid, setInvalid] = useState(false);
  // The URL the shell flagged as "doesn't look like Omnigent" — a second
  // connect on the same URL proceeds (force); editing the input clears it.
  const [unconfirmedUrl, setUnconfirmedUrl] = useState<string | null>(null);
  // Message from a rejected connect, so a failed Join shows something.
  const [connectError, setConnectError] = useState<string | null>(null);
  // Advisory per-server reachability status (added servers only).
  const [checks, setChecks] = useState<Record<string, CheckStatus>>({});
  // The server whose detail accordion is expanded, or null (one at a time).
  const [expandedUrl, setExpandedUrl] = useState<string | null>(null);
  // "list": pick from existing servers (with an "Add server" button). "add":
  // the URL-input view (heading + input + benefits). Empty list → start in add.
  const [mode, setMode] = useState<"list" | "add">(listed.length === 0 ? "add" : "list");

  // Tell the parent when the add (URL-input) view is showing, so it can swap the
  // panel band to the hero icons.
  useEffect(() => {
    onAddModeChange?.(mode === "add");
  }, [mode, onAddModeChange]);

  // Default-select the first listed server on mount (mirrors "a recent is
  // pre-selected"), but not while the user is composing a new URL.
  const firstListed = managedServers[0] ?? recentServers[0] ?? null;
  useEffect(() => {
    if (firstListed !== null)
      setSelected((prev) => (prev === null && typedUrl === "" ? firstListed : prev));
    // typedUrl intentionally omitted: only seed once from the arriving list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstListed]);

  const clearInputState = () => {
    setInvalid(false);
    setUnconfirmedUrl(null);
    setConnectError(null);
  };

  // Pick a listed server (exclusive with the input).
  const select = (url: string) => {
    setSelected(url);
    setTypedUrl("");
    clearInputState();
  };

  // Add the typed URL to the list, select it, and fire an advisory reachability
  // probe. Join is immediately usable on the added server; the check is async
  // and never blocks it.
  const addServer = () => {
    const url = normalizeServerUrl(typedUrl);
    if (url === null) {
      setInvalid(true);
      return;
    }
    if (!listed.includes(url)) setAddedServers((prev) => [url, ...prev]);
    setSelected(url);
    setTypedUrl("");
    clearInputState();
    setMode("list");
    setChecks((prev) => ({ ...prev, [url]: "checking" }));
    onCheckServer(url)
      .then((r) => setChecks((prev) => ({ ...prev, [url]: r.status })))
      .catch(() => setChecks((prev) => ({ ...prev, [url]: "unreachable" })));
  };

  // Connect to the selected server. Second click on an unconfirmed URL forces.
  const join = async () => {
    if (selected === null) return;
    setConnectError(null);
    const force = unconfirmedUrl === selected;
    const result = await onConnect(selected, force);
    setUnconfirmedUrl(result.needsConfirm ? selected : null);
    setConnectError(result.error ?? null);
  };

  const removeServer = (url: string) => {
    onRemove?.(url);
    setAddedServers((prev) => prev.filter((u) => u !== url));
    if (selected === url) setSelected(null);
  };

  // Session-added servers are just recents the user hasn't connected to yet.
  const recentSection = [...addedServers, ...recentServers];

  const renderRow = (url: string) => {
    const isSelected = selected === url;
    const removable = onRemove != null && !managedServers.includes(url);
    const check = checks[url];
    const isFirstRecent = url === recentServers[0] && addedServers.length === 0;
    const isExpanded = expandedUrl === url;
    return (
      <div
        key={url}
        className={cn(
          "flex flex-col rounded-lg border-2 px-3 py-2.5 transition-[border-color,background-color]",
          isSelected ? "border-primary bg-primary/5" : "border-border hover:bg-muted",
        )}
      >
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => select(url)}
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
                {isFirstRecent && (
                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[11px] leading-none">
                    Last used
                  </span>
                )}
                {check ? (
                  <span className={cn("truncate", check === "unreachable" && "text-destructive")}>
                    {check === "checking"
                      ? "Checking…"
                      : check === "ok"
                        ? "Omnigent server"
                        : check === "reachable"
                          ? "Reachable"
                          : "Can't reach"}
                  </span>
                ) : (
                  <span className="truncate">{isLocal(url) ? "Local" : "Remote"}</span>
                )}
              </span>
            </span>
          </button>

          <button
            type="button"
            onClick={() => setExpandedUrl((prev) => (prev === url ? null : url))}
            className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
            aria-label={`${isExpanded ? "Collapse" : "Expand"} ${displayName(url)}`}
            aria-expanded={isExpanded}
          >
            <ChevronDown
              className={cn("size-4 transition-transform", isExpanded && "rotate-180")}
              aria-hidden
            />
          </button>

          {removable && (
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
                <DropdownMenuItem variant="destructive" onSelect={() => removeServer(url)}>
                  <Trash2 className="size-4" aria-hidden />
                  Delete from list
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        {isExpanded && <ServerDetails url={url} local={isLocal(url)} onCopy={onCopy} />}
      </div>
    );
  };

  return (
    <div className="flex h-full flex-col px-2 pb-1">
      <div className="pt-8">
        <OnboardingHeading>Join your team</OnboardingHeading>
      </div>

      {(error || invalid || unconfirmedUrl || connectError) && (
        <div role="alert" className="mb-2 text-base text-destructive">
          {invalid ? (
            "Enter a valid http(s) server URL."
          ) : connectError ? (
            <>
              <span className="font-medium">Couldn&apos;t connect to the server: </span>
              {connectError}
            </>
          ) : unconfirmedUrl ? (
            "This doesn't look like an Omnigent server. Click Join again to connect anyway."
          ) : (
            <>
              <span className="font-medium">Couldn&apos;t connect to the server: </span>
              {error}
            </>
          )}
        </div>
      )}

      {mode === "add" ? (
        <div className="flex min-h-0 flex-1 flex-col gap-4">
          {/* One paragraph of what joining unlocks (design: sentences, not a list). */}
          <p className="text-base text-muted-foreground max-w-sm mx-auto text-center">
            Access agents from any device. Share sessions with your teammates. Keep sessions running
            in the cloud.
          </p>

          <div className="flex  items-center gap-2">
            {/* URL input with an inline Join on the right — no separate submit below. */}
            <div className="flex flex-1 items-center gap-2 rounded-lg border border-border px-3">
              <Input
                value={typedUrl}
                onChange={(e) => {
                  setTypedUrl(e.target.value);
                  setSelected(null);
                  clearInputState();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addServer();
                }}
                placeholder="Enter Omnigent server URL"
                className="border-0 px-0 shadow-none focus-visible:ring-0"
                aria-label="Server URL"
              />
            </div>
            <Button type="button" disabled={typedUrl.trim().length === 0} onClick={addServer}>
              Join
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto">
          {managedServers.length > 0 && (
            <section className="flex flex-col gap-2">
              <SectionHeader label="Preset (by your organization)" />
              {managedServers.map(renderRow)}
            </section>
          )}
          <section className="flex flex-col gap-2">
            <SectionHeader
              label="Recents"
              action={
                <button
                  type="button"
                  onClick={() => {
                    setSelected(null);
                    setMode("add");
                  }}
                  className="flex shrink-0 items-center gap-1 text-base text-muted-foreground hover:text-foreground"
                >
                  <Plus className="size-4" aria-hidden />
                  Add server
                </button>
              }
            />
            {recentSection.map(renderRow)}
          </section>
        </div>
      )}

      <div className="mt-3 flex justify-between gap-2">
        {mode === "add" ? (
          <>
            <Button
              variant="ghost"
              size="lg"
              // Back returns to the list when there is one to return to; from
              // the empty-list add view it exits the step.
              onClick={() => (listed.length > 0 ? setMode("list") : onBack())}
            >
              <ArrowLeft className="size-4" />
              Back
            </Button>
            {/* No submit here — Join lives in the input row. This is the
                escape hatch to spin up a server instead. */}
            <Button variant="ghost" asChild size="lg">
              <a href={CREATE_SERVER_URL} target="_blank" rel="noreferrer">
                Create server
                <SquareArrowOutUpRight className="size-4" aria-hidden />
              </a>
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={onBack} size="lg">
              <ArrowLeft className="size-4" />
              Back
            </Button>
            <Button disabled={selected === null} onClick={join} size="lg">
              {installActionLabel(installed)}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Inline server-detail accordion body (expanded row). Org / admin / last-used /
 * participants are mock placeholders — the setup bridge doesn't expose them
 * yet; the design shows them, so we display placeholders for remote servers.
 */
/** One label→value row with a leading icon (detail accordion). */
function DetailRow({
  icon: Icon,
  label,
  children,
}: {
  icon: ComponentType<LucideProps>;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1">
      <span className="flex items-center gap-1 text-muted-foreground">
        <Icon className="size-3.5 shrink-0" aria-hidden />
        <span>{label}</span>
      </span>
      {children}
    </div>
  );
}

/** Inline server-detail body (icon-labeled rows). Shared by the list accordion
 *  and the single-server detail step. Mock org/admin fields for remote servers. */
export function ServerDetails({
  url,
  local,
  onCopy,
}: {
  url: string;
  local: boolean;
  onCopy: (text: string) => void;
}) {
  const name = displayName(url);
  return (
    <div className="mt-2.5 flex flex-col border-t border-border pt-2.5 text-base">
      {!local && (
        <>
          <DetailRow icon={Building} label="Organization">
            <span className="truncate text-right text-foreground">Databricks</span>
          </DetailRow>
          <DetailRow icon={ShieldUser} label="Admin">
            <span className="truncate text-right text-foreground">Ajay Alfred</span>
          </DetailRow>
          <DetailRow icon={Timer} label="Last used">
            <span className="text-foreground">2m</span>
          </DetailRow>
          <DetailRow icon={Users} label="Participants">
            <span className="text-foreground">42</span>
          </DetailRow>
        </>
      )}
      <DetailRow icon={Server} label="Server URL">
        <span className="flex min-w-0 items-center gap-1">
          <span className="truncate text-right text-foreground">{name}</span>
          <button
            type="button"
            onClick={() => onCopy(url)}
            className="flex size-3.5 shrink-0 items-center justify-center text-muted-foreground hover:text-foreground"
            aria-label={`Copy ${name}`}
          >
            <Copy className="size-3.5" aria-hidden />
          </button>
        </span>
      </DetailRow>
    </div>
  );
}
