import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  ChevronDownIcon,
  KeyRoundIcon,
  Plus,
  SearchIcon,
  SettingsIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Link, useNavigate, useSearchParams } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { ComposerAgentIcon } from "@/shell/NewChatDialog";
import { useHarnessStartup, useHosts, type Host } from "@/hooks/useHosts";
import { ApiError } from "@/lib/sessionsApi";
import { INVENTORY_HARNESS_IDS } from "@/hooks/useHarnessInventory";
import { BRAND_HARNESSES } from "@/components/onboarding/harnessBrand";
import {
  HARNESS_CREDENTIAL_COPY,
  harnessInstallableOnHost,
  harnessReadinessOnHost,
  harnessWarningBadgeText,
  type HarnessCredentialKind,
} from "@/lib/harnessSetup";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";
import { BackButton, HarnessCatalog } from "./HarnessCatalog";
import { HarnessFixDialog, type HarnessFixMode } from "./HarnessFixDialog";
import { useSettingsRoute } from "../../shell/settingsNav";

interface HarnessEntry {
  /** Native harness slug (e.g. "claude-native") — the readiness/install key. */
  harness: string;
  name: string;
  description: string;
  agentName: string;
}

// One-line descriptions per harness, keyed by native slug. The server catalog
// (/v1/harnesses) carries no descriptions, so these live here; the rest of each
// entry (name, icon, order) comes from NATIVE_CODING_AGENTS.
const HARNESS_DESCRIPTIONS: Record<string, string> = {
  "claude-native":
    "Anthropic’s coding agent for understanding codebases, editing files, and running development workflows.",
  "codex-native":
    "OpenAI’s coding agent for building features, fixing bugs, and working across repositories.",
  "opencode-native": "An open-source coding agent for the terminal, IDE, and desktop.",
  "cursor-native":
    "An AI code editor with agent workflows for navigating, editing, and shipping code.",
  "pi-native": "A minimal, extensible coding agent harness built for terminal workflows.",
  "devin-native": "Cognition’s autonomous coding agent for end-to-end software tasks.",
  "antigravity-native":
    "Google’s agent-first development platform for planning and executing software tasks.",
  "kiro-native":
    "An agentic IDE for spec-driven development, hooks, and production-ready software.",
  "qwen-native": "An open-source terminal coding agent powered by Qwen models.",
  "goose-native": "An open-source local AI agent for coding, automation, and extensible workflows.",
  "kimi-native":
    "A terminal coding agent for editing code, running commands, and completing development tasks.",
  "hermes-native": "A self-improving AI agent that learns reusable skills from experience.",
};

// The harness catalog, derived from the shared native-agent registry so this
// list stays in step with the rest of the app; descriptions come from the map
// above. Sorted by the registry's own picker order.
const HARNESS_ENTRIES: HarnessEntry[] = [...NATIVE_CODING_AGENTS]
  .sort((a, b) => a.sortRank - b.sortRank)
  .map((spec) => ({
    harness: spec.harness,
    name: spec.displayName,
    agentName: spec.agentName,
    description: HARNESS_DESCRIPTIONS[spec.harness] ?? "",
  }));

function hostHarnessKey(hostId: string, harness: string): string {
  return `${hostId}:${harness}`;
}

/**
 * Settings → Harnesses: the harness grid at /settings/harnesses, or one
 * harness's details at /settings/harnesses/<harness>. An unknown slug shows the
 * grid. Host selection lives here so it survives grid ↔ details navigation.
 */
export const SettingsHarnessesSection = () => {
  const { harness } = useSettingsRoute();
  const [query, setQuery] = useState("");
  const [installedOnly, setInstalledOnly] = useState(false);
  const info = useServerInfo();
  const queryClient = useQueryClient();
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const [searchParams] = useSearchParams();
  const requestedHostId = searchParams.get("host");
  const [selectedHostId, setSelectedHostId] = useState<string | null>(requestedHostId);
  // In-section links drop `?host`, so only a new deep link replaces the pick.
  useEffect(() => {
    if (requestedHostId !== null) setSelectedHostId(requestedHostId);
  }, [requestedHostId]);
  const [credentialKinds, setCredentialKinds] = useState<Record<string, HarnessCredentialKind>>({});
  const [readyNotice, setReadyNotice] = useState<{
    harness: string;
    hostId: string;
    version?: string;
  } | null>(null);

  // Gate the "Set up" affordance on the install feature, matching New Chat: with
  // it off the setup dialog has no runnable install step, so we show status
  // only (no button that opens a dead-end dialog).
  const canSetup = isFeatureEnabled(info, "harness_install");

  // Online hosts first, then offline (disabled in the menu). Default the
  // selection to the first online host; the picker can override.
  const sortedHosts = useMemo(
    () =>
      [...(hosts ?? [])].sort(
        (a, b) => Number(b.status === "online") - Number(a.status === "online"),
      ),
    [hosts],
  );
  const onlineHosts = useMemo(
    () => sortedHosts.filter((h) => h.status === "online"),
    [sortedHosts],
  );
  const selectedHost =
    onlineHosts.find((h) => h.host_id === selectedHostId) ?? onlineHosts[0] ?? null;

  // Capture the selected host when the fix opens so a later host change cannot
  // redirect an in-progress install or credential write to another machine.
  const [fixTarget, setFixTarget] = useState<{
    entry: HarnessEntry;
    host: Host;
    mode: HarnessFixMode;
  } | null>(null);
  const openFix = (entry: HarnessEntry, targetHost: Host, mode: HarnessFixMode) =>
    setFixTarget({ entry, host: targetHost, mode });

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return HARNESS_ENTRIES.filter(
      (h) =>
        (!installedOnly || harnessStatus(h.harness, selectedHost).ready) &&
        (!q || h.name.toLowerCase().includes(q) || h.description.toLowerCase().includes(q)),
    );
  }, [query, installedOnly, selectedHost]);

  const detail = HARNESS_ENTRIES.find((entry) => entry.harness === harness);

  return (
    <div className="@container">
      {detail ? (
        <HarnessDetail
          key={detail.harness}
          entry={detail}
          host={selectedHost}
          canSetup={canSetup}
          installable={harnessInstallableOnHost(info, detail.harness, selectedHost)}
          credentialKind={
            selectedHost
              ? credentialKinds[hostHarnessKey(selectedHost.host_id, detail.harness)]
              : undefined
          }
          readyNotice={
            readyNotice !== null &&
            readyNotice.hostId === selectedHost?.host_id &&
            readyNotice.harness === detail.harness
              ? readyNotice
              : null
          }
          onFix={(mode) => selectedHost && openFix(detail, selectedHost, mode)}
        />
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-4 pb-6">
            <h1 className="settings-page-title text-2xl font-semibold">Harnesses</h1>
            <HostSelect hosts={sortedHosts} selected={selectedHost} onSelect={setSelectedHostId} />
          </div>
          <div className="mb-6 flex items-center gap-2">
            <div className="flex h-8 flex-1 items-center gap-2 rounded-lg border border-border px-2.5">
              <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search agent harnesses..."
                aria-label="Search agent harnesses"
                data-testid="harness-search"
                className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
              />
            </div>
            <Tabs
              value={installedOnly ? "installed" : "all"}
              onValueChange={(v) => setInstalledOnly(v === "installed")}
            >
              <TabsList>
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="installed" data-testid="harness-filter-installed">
                  Configured
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          {!selectedHost && <NoHostNotice />}
          {filtered.length === 0 && (
            <p className="text-ui text-muted-foreground">
              {query.trim() ? `No harnesses match “${query}”.` : "No installed harnesses."}
            </p>
          )}
          <div
            className={cn(
              "grid grid-cols-1 gap-3 @[520px]:grid-cols-2",
              // No host to resolve status against — dim the catalog to read as inactive.
              !selectedHost && "opacity-50",
            )}
          >
            {filtered.map((entry) => (
              <HarnessCard
                key={entry.harness}
                entry={entry}
                host={selectedHost}
                canSetup={canSetup}
                installable={harnessInstallableOnHost(info, entry.harness, selectedHost)}
                credentialKind={
                  selectedHost
                    ? credentialKinds[hostHarnessKey(selectedHost.host_id, entry.harness)]
                    : undefined
                }
                onFix={(mode) => selectedHost && openFix(entry, selectedHost, mode)}
              />
            ))}
          </div>
        </>
      )}
      {fixTarget && (
        <HarnessFixDialog
          open
          onOpenChange={(open) => !open && setFixTarget(null)}
          mode={fixTarget.mode}
          host={fixTarget.host}
          harness={fixTarget.entry.harness}
          harnessLabel={fixTarget.entry.name}
          onFixed={({ configuredHarnesses, credentialKind, version, gatewayInference }) => {
            const { host: fixedHost, entry } = fixTarget;
            queryClient.setQueriesData<Host[]>({ queryKey: ["hosts"] }, (cachedHosts) =>
              cachedHosts?.map((cachedHost) =>
                cachedHost.host_id === fixedHost.host_id
                  ? {
                      ...cachedHost,
                      configured_harnesses: configuredHarnesses,
                      ...(gatewayInference !== undefined
                        ? { gateway_inference: gatewayInference }
                        : {}),
                    }
                  : cachedHost,
              ),
            );
            if (credentialKind) {
              setCredentialKinds((current) => ({
                ...current,
                [hostHarnessKey(fixedHost.host_id, entry.harness)]: credentialKind,
              }));
            }
            setReadyNotice({ harness: entry.harness, hostId: fixedHost.host_id, version });
          }}
        />
      )}
    </div>
  );
};

function NoHostNotice() {
  return (
    <p className="text-ui text-muted-foreground" data-testid="harness-no-host">
      Connect an online host to see which harnesses are installed and to set them up.
    </p>
  );
}

/** Online/offline status dot, mirroring the composer host rows. */
function HostStatusDot({ online }: { online: boolean }) {
  return (
    <span
      aria-hidden
      className={cn(
        "size-2 shrink-0 rounded-full",
        online ? "bg-success" : "border-[1.5px] border-muted-foreground",
      )}
    />
  );
}

/**
 * Host picker for the Harnesses grid — a compact status pill that opens a menu
 * of hosts (online selectable, offline disabled). Mirrors the composer's host
 * rows (status dot + name) without its connect-new-host / sandbox affordances.
 */
function HostSelect({
  hosts,
  selected,
  onSelect,
}: {
  hosts: Host[];
  selected: Host | null;
  onSelect: (hostId: string) => void;
}) {
  if (hosts.length === 0) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="text-ui text-muted-foreground">Machine</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            size="sm"
            className="h-8 shrink-0 gap-1.5"
            data-testid="harness-host-select"
            componentId="settings.harnesses.host"
          >
            <HostStatusDot online={selected?.status === "online"} />
            <span className="max-w-40 truncate">{selected?.name ?? "Select a host"}</span>
            <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-52">
          {hosts.map((h) => {
            const online = h.status === "online";
            return (
              <DropdownMenuItem
                key={h.host_id}
                disabled={!online}
                onSelect={() => online && onSelect(h.host_id)}
                data-active={h.host_id === selected?.host_id ? "true" : undefined}
                data-testid={`harness-host-${h.host_id}`}
                className="gap-1.5 data-[active=true]:bg-muted dark:data-[active=true]:bg-muted/50"
              >
                <HostStatusDot online={online} />
                <span className="min-w-0 flex-1 truncate">{h.name}</span>
                {!online && <span className="text-xs text-muted-foreground">Offline</span>}
              </DropdownMenuItem>
            );
          })}
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/** Readiness of *harness* on *host*, reduced to what the grid and details show. */
function harnessStatus(harness: string, host: Host | null) {
  const readiness = harnessReadinessOnHost(harness, host);
  return {
    ready: readiness.state === "available" && readiness.reason === "ready",
    // Only "setup-required" / "broken" are actionable. A host that reports no
    // readiness (older host → `readiness-unknown`) stays neutral: no badge, no
    // Set-up button, rather than a false "needs setup" on a working harness.
    needsSetup: readiness.state === "setup-required" || readiness.state === "broken",
    reason:
      readiness.reason === "ready" || readiness.reason === "readiness-unknown"
        ? null
        : readiness.reason,
  };
}

function credentialPresentation(
  host: Host | null,
  harness: string,
  override?: HarnessCredentialKind,
) {
  if (override === "personal") {
    return HARNESS_CREDENTIAL_COPY.personal;
  }
  if (override === "isaac" || host?.gateway_inference?.[harness] === true) {
    return HARNESS_CREDENTIAL_COPY.isaac;
  }
  return { label: "Signed in", description: "Uses the harness's own login on this host" };
}

function reconfigurationReason(reason: string | null) {
  if (reason === "needs-auth") return "Missing credentials";
  if (reason === "version-too-low") return "Out of date";
  if (reason === "binary-missing") return "Not installed";
  if (reason === "readiness-error") return "Readiness check failed";
  if (reason === "unconfigured") return "Not configured";
  const fallback = harnessWarningBadgeText(reason);
  return fallback.charAt(0).toUpperCase() + fallback.slice(1);
}

function HarnessStatusText({
  status,
  configuredCredential,
  enhanced,
}: {
  status: ReturnType<typeof harnessStatus>;
  configuredCredential?: string;
  enhanced: boolean;
}) {
  if (status.ready) {
    return (
      <span className="flex items-center gap-1.5 text-xs">
        <span className="text-green-600 dark:text-green-400">Configured</span>
        {configuredCredential && (
          <span className="text-muted-foreground">· {configuredCredential}</span>
        )}
      </span>
    );
  }
  if (status.needsSetup && !enhanced) {
    const text = harnessWarningBadgeText(status.reason);
    return (
      <span className="text-xs text-amber-600 dark:text-amber-500">
        {text.charAt(0).toUpperCase() + text.slice(1)}
      </span>
    );
  }
  if (status.needsSetup) {
    if (status.reason === "binary-missing" || status.reason === "unconfigured") {
      return (
        <span className="flex w-fit items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
          <TriangleAlertIcon className="size-3.5" aria-hidden />
          {reconfigurationReason(status.reason)}
        </span>
      );
    }
    const label = reconfigurationReason(status.reason);
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className="flex w-fit cursor-default items-center gap-1 text-xs text-amber-600 dark:text-amber-500"
            aria-label={`Needs reconfiguration: ${label}`}
          >
            <TriangleAlertIcon className="size-3.5" aria-hidden />
            Needs reconfiguration
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    );
  }
  return null;
}

function HarnessIcon({ entry }: { entry: HarnessEntry }) {
  return (
    <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border [&_img]:size-5 [&_svg]:size-5">
      <ComposerAgentIcon agent={{ name: entry.agentName, harness: entry.harness }} />
    </div>
  );
}

function SetupButton({ entry, onSetup }: { entry: HarnessEntry; onSetup: () => void }) {
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className="shrink-0"
      aria-label={`Set up ${entry.name}`}
      data-testid={`harness-action-${entry.harness}`}
      componentId="settings.harnesses.setup"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onSetup();
      }}
    >
      <Plus />
      {/* Set up */}
    </Button>
  );
}

/** Grid card. An installed harness's card opens its details page. */
function HarnessCard({
  entry,
  host,
  canSetup,
  installable,
  credentialKind,
  onFix,
}: {
  entry: HarnessEntry;
  host: Host | null;
  canSetup: boolean;
  installable: boolean;
  credentialKind?: HarnessCredentialKind;
  onFix: (mode: HarnessFixMode) => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const mode = fixMode(status.reason);
  const canFix = canSetup && !!host && !host.sandbox_provider;
  const opensDetail = status.ready || (status.needsSetup && canFix);
  const navigate = useNavigate();
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-ui font-medium text-foreground">{entry.name}</span>
            <HarnessStatusText
              status={status}
              enhanced={canSetup}
              configuredCredential={
                status.ready && canSetup
                  ? credentialPresentation(host, entry.harness, credentialKind).label
                  : undefined
              }
            />
          </div>
        </div>
        {(status.ready ||
          (status.needsSetup && canFix && (mode !== "install" || !installable))) && (
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label={`${entry.name} settings`}
            data-testid={`harness-settings-${entry.harness}`}
            componentId="settings.harnesses.open_settings"
            onClick={(e) => {
              // Inside the card's link: don't also follow it to the MCP servers tab.
              e.preventDefault();
              navigate(`/settings/harnesses/${entry.harness}?tab=settings`);
            }}
          >
            <SettingsIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          </Button>
        )}
        {status.needsSetup && canFix && installable && mode === "install" && (
          <SetupButton entry={entry} onSetup={() => onFix("install")} />
        )}
      </div>
      <p className="line-clamp-2 text-ui text-muted-foreground">{entry.description}</p>
    </>
  );
  const className =
    "flex flex-col gap-2 rounded-[20px] border border-border bg-card p-4 transition-colors";
  return opensDetail ? (
    <Link
      to={`/settings/harnesses/${entry.harness}${status.ready ? "" : "?tab=settings"}`}
      // Only cards that open a details page get the hover, so it reads as clickable.
      className={cn(
        className,
        "hover:border-foreground/20 hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
      )}
      data-testid={`harness-card-${entry.harness}`}
      componentId="settings.harnesses.open"
    >
      {body}
    </Link>
  ) : (
    <div className={className} data-testid={`harness-card-${entry.harness}`}>
      {body}
    </div>
  );
}

/** Details page for one harness on the selected host. */
function HarnessDetail({
  entry,
  host,
  canSetup,
  installable,
  credentialKind,
  readyNotice,
  onFix,
}: {
  entry: HarnessEntry;
  host: Host | null;
  canSetup: boolean;
  installable: boolean;
  credentialKind?: HarnessCredentialKind;
  readyNotice: { harness: string; hostId: string; version?: string } | null;
  onFix: (mode: HarnessFixMode) => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const mode = fixMode(status.reason);
  const canFix = canSetup && !!host && !host.sandbox_provider;
  const gateway = host?.gateway_inference?.[entry.harness] === true;
  const credential = canSetup
    ? credentialPresentation(host, entry.harness, credentialKind)
    : gateway
      ? {
          label: "Unity Gateway",
          description: "Managed credential via the Databricks Unity Gateway",
        }
      : { label: "Signed in", description: "Uses the harness's own login on this host" };
  const header = (
    <>
      <BackButton label="Harnesses" to="/settings/harnesses" />
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <h1 className="settings-page-title truncate text-2xl font-semibold">{entry.name}</h1>
            <HarnessStatusText
              status={status}
              enhanced={canSetup}
              configuredCredential={status.ready && canSetup ? credential.label : undefined}
            />
          </div>
        </div>
      </div>
      {host && canSetup && <MachineField host={host} />}
    </>
  );
  const credentialCard = (
    <CredentialCard
      label={credential.label}
      description={credential.description}
      ready={status.ready}
      actionable={status.needsSetup && canFix}
      showStatus={canSetup}
      statusLabel={
        status.reason === "binary-missing" || status.reason === "unconfigured"
          ? reconfigurationReason(status.reason)
          : "Needs reconfiguration"
      }
      onClick={() => onFix(mode)}
    />
  );
  const settings = host ? (
    <div className="flex flex-col gap-6">
      {readyNotice && <ReadyNotice entry={entry} host={host} version={readyNotice.version} />}
      {status.needsSetup && (
        <HarnessWarning
          entry={entry}
          host={host}
          mode={mode}
          reason={status.reason}
          installable={installable}
          onFix={onFix}
        />
      )}
      {credentialCard}
      <StartupSettings host={host} harness={entry.harness} />
    </div>
  ) : null;
  // The host inventory only covers these families' MCP servers, skills, and plugins.
  const family = BRAND_HARNESSES.find((f) => INVENTORY_HARNESS_IDS[f] === entry.harness);
  if (host && family && (status.ready || canFix)) {
    return (
      <HarnessCatalog
        header={header}
        settings={settings}
        host={host}
        family={family}
        defaultTab={status.ready ? "mcps" : "settings"}
      />
    );
  }
  return (
    <>
      {header}
      <div className="mt-6 flex flex-col gap-6">
        {!host ? (
          <NoHostNotice />
        ) : status.ready ? (
          <>
            <p className="text-ui text-muted-foreground">
              MCP servers, skills, and plugins aren't listed for {entry.name} yet.
            </p>
            {readyNotice && <ReadyNotice entry={entry} host={host} version={readyNotice.version} />}
            {credentialCard}
          </>
        ) : canFix ? (
          <>
            <HarnessWarning
              entry={entry}
              host={host}
              mode={mode}
              reason={status.reason}
              installable={installable}
              onFix={onFix}
            />
            {credentialCard}
            <StartupSettings host={host} harness={entry.harness} />
          </>
        ) : (
          <p className="text-ui text-muted-foreground">
            Set up {entry.name} on {host.name} to manage its MCP servers, skills, and plugins.
          </p>
        )}
      </div>
    </>
  );
}

function fixMode(reason: string | null): HarnessFixMode {
  if (reason === "version-too-low") return "update";
  if (reason === "binary-missing" || reason === "unconfigured") return "install";
  return "reconfigure";
}

function MachineField({ host }: { host: Host }) {
  return (
    <div className="mt-6 grid max-w-xl grid-cols-[8rem_1fr] items-center gap-3">
      <span className="text-ui text-muted-foreground">Machine</span>
      <div className="rounded-lg border border-border bg-muted/30 px-3 py-2 text-ui">
        {host.name}
      </div>
    </div>
  );
}

function HarnessWarning({
  entry,
  host,
  mode,
  reason,
  installable,
  onFix,
}: {
  entry: HarnessEntry;
  host: Host;
  mode: HarnessFixMode;
  reason: string | null;
  installable: boolean;
  onFix: (mode: HarnessFixMode) => void;
}) {
  const copy =
    reason === "readiness-error"
      ? `${entry.name} isn’t working on ${host.name}. Run setup again to fix it.`
      : mode === "reconfigure"
        ? `${entry.name} can’t sign in on ${host.name}. Its credential is missing, so new sessions can’t use ${entry.name}.`
        : mode === "update"
          ? `${entry.name} is out of date on ${host.name}. Update it so new sessions can use ${entry.name}.`
          : `${entry.name} isn’t set up on ${host.name}, so new sessions can’t use ${entry.name}.`;
  const action = !installable
    ? "Set up"
    : mode === "update"
      ? "Update"
      : mode === "install"
        ? "Install"
        : "Reconfigure";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-ui">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-500" />
        <p>{copy}</p>
      </div>
      {!host.sandbox_provider && (
        <Button size="sm" onClick={() => onFix(mode)}>
          {action}
        </Button>
      )}
    </div>
  );
}

function ReadyNotice({
  entry,
  host,
  version,
}: {
  entry: HarnessEntry;
  host: Host;
  version?: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-green-500/30 bg-green-500/10 p-4 text-ui text-green-700 dark:text-green-300">
      <CheckCircle2Icon className="size-4 shrink-0" aria-hidden />
      <p>
        {entry.name}
        {version ? ` ${version}` : ""} is ready on {host.name}. Checked just now.
      </p>
    </div>
  );
}

function StartupSettings({ host, harness }: { host: Host; harness: string }) {
  const { data, error, isPending } = useHarnessStartup(host.host_id, harness);
  if (error instanceof ApiError && error.status === 404) return null;
  if (isPending) return <p className="text-ui text-muted-foreground">Loading launch settings…</p>;
  if (error || !data) {
    return (
      <p className="text-ui text-muted-foreground">
        {error instanceof ApiError && error.status === 501
          ? `Update ${host.name} to see launch settings.`
          : `Couldn't load launch settings from ${host.name}.`}
      </p>
    );
  }
  const source = {
    env: `From OMNIGENT_${harness.replace(/-native$/, "").toUpperCase()}_PATH on ${host.name}.`,
    config: `From harness.${harness} in ~/.omnigent/config.yaml on ${host.name}.`,
    default: `Default command on ${host.name}.`,
  }[data.command_source];
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          Startup configuration
        </h2>
        <p className="text-xs text-muted-foreground">
          {source} Read-only host defaults, not a running session's full command or environment.
          Sessions and workspaces may override these.
        </p>
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Command</h3>
        <code className="rounded-xl border border-border p-3 text-ui break-all">
          {data.resolved_path ?? data.command}
        </code>
        {!data.resolved_path && (
          <p className="text-xs text-muted-foreground">Executable not found.</p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Environment</h3>
        {data.environment ? (
          <>
            {Object.keys(data.environment.variables).length > 0 ? (
              <dl className="flex flex-col gap-2 rounded-xl border border-border p-3 text-ui">
                {Object.entries(data.environment.variables).map(([name, value]) => (
                  <div key={name} className="flex flex-wrap items-baseline gap-x-3">
                    <dt className="font-mono break-all">{name}</dt>
                    <dd>
                      <code className="whitespace-pre-wrap break-all">{value || '""'}</code>
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="rounded-xl border border-border p-3 text-ui">None configured</p>
            )}
            <p className="text-xs text-muted-foreground">
              {data.environment.inherit
                ? "Inherited values are not listed."
                : "Inherited environment cleared."}
            </p>
            {data.environment.unset.length > 0 && (
              <p className="text-xs text-muted-foreground">
                Removed before applying overrides: <code>{data.environment.unset.join(", ")}</code>.
              </p>
            )}
          </>
        ) : (
          <p className="text-ui text-muted-foreground">
            {data.configured_args == null
              ? `Update ${host.name} to see environment settings.`
              : "Cannot separate environment values for this env wrapper. Command and arguments are shown unchanged."}
          </p>
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="text-ui font-medium">Arguments</h3>
        {data.args == null ? (
          <p className="rounded-xl border border-border p-3 text-ui">
            {data.arg_count === 0
              ? "None configured"
              : `${data.arg_count} configured argument${data.arg_count === 1 ? "" : "s"}. Update ${host.name} to view values.`}
          </p>
        ) : (
          <StartupArguments args={data.args} />
        )}
      </div>
    </section>
  );
}

function StartupArguments({ args }: { args: string[] }) {
  if (args.length === 0)
    return <p className="rounded-xl border border-border p-3 text-ui">None configured</p>;
  return (
    <ol className="flex list-decimal flex-col gap-1 rounded-xl border border-border py-3 pr-3 pl-9 text-ui">
      {args.map((arg, index) => (
        // eslint-disable-next-line react/no-array-index-key
        <li key={index}>
          <code className="whitespace-pre-wrap break-all">{arg || '""'}</code>
        </li>
      ))}
    </ol>
  );
}

/** Where the harness's login comes from. */
function CredentialCard({
  label,
  description,
  ready,
  actionable,
  showStatus,
  statusLabel,
  onClick,
}: {
  label: string;
  description: string;
  ready: boolean;
  actionable: boolean;
  showStatus: boolean;
  statusLabel: string;
  onClick: () => void;
}) {
  const body = (
    <>
      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted">
        <KeyRoundIcon className="size-4 text-muted-foreground" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col text-left">
        <span className="text-ui font-medium">{label}</span>
        <span className="text-xs text-muted-foreground">{description}</span>
      </span>
      {showStatus && (
        <span
          className={cn(
            "text-xs",
            ready ? "text-green-600 dark:text-green-400" : "text-amber-600 dark:text-amber-500",
          )}
        >
          {ready ? "Configured" : statusLabel}
        </span>
      )}
    </>
  );
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Credential
      </h2>
      {actionable ? (
        <button
          type="button"
          aria-label="Reconfigure credential"
          className="flex w-full items-center gap-3 rounded-xl border border-border p-3 transition-colors hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
          onClick={onClick}
        >
          {body}
        </button>
      ) : (
        <div className="flex items-center gap-3 rounded-xl border border-border p-3">{body}</div>
      )}
    </section>
  );
}
