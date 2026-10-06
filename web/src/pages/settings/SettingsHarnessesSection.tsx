import { useMemo, useState } from "react";
import { ChevronDownIcon, KeyRoundIcon, Plus, SearchIcon, SettingsIcon } from "lucide-react";
import { Link, useNavigate } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { ComposerAgentIcon } from "@/shell/NewChatDialog";
import { HarnessSetupDialog } from "@/shell/HarnessSetupDialog";
import { useHarnessStartup, useHosts, type Host } from "@/hooks/useHosts";
import { ApiError } from "@/lib/sessionsApi";
import { INVENTORY_HARNESS_IDS } from "@/hooks/useHarnessInventory";
import { BRAND_HARNESSES } from "@/components/onboarding/harnessBrand";
import {
  harnessReadinessOnHost,
  harnessUnavailableReasonOnHost,
  harnessWarningBadgeText,
} from "@/lib/harnessSetup";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";
import { BackButton, HarnessCatalog } from "./HarnessCatalog";
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
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);

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
  const host = onlineHosts.find((h) => h.host_id === selectedHostId) ?? onlineHosts[0] ?? null;

  // Setup dialog target: reuses the composer's install + auth flow. Captures
  // BOTH the harness and the host chosen when setup opened, so a later host
  // switch (selection change or the selected host going offline) can't redirect
  // an in-progress install / credential write to a different machine.
  const [setupTarget, setSetupTarget] = useState<{ entry: HarnessEntry; host: Host } | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return HARNESS_ENTRIES.filter(
      (h) =>
        (!installedOnly || harnessStatus(h.harness, host).ready) &&
        (!q || h.name.toLowerCase().includes(q) || h.description.toLowerCase().includes(q)),
    );
  }, [query, installedOnly, host]);

  const detail = HARNESS_ENTRIES.find((entry) => entry.harness === harness);

  return (
    <div className="@container">
      {detail ? (
        <HarnessDetail
          key={detail.harness}
          entry={detail}
          host={host}
          canSetup={canSetup}
          onSetup={() => host && setSetupTarget({ entry: detail, host })}
        />
      ) : (
        <>
          <div className="flex flex-wrap items-center justify-between gap-4 pb-6">
            <h1 className="settings-page-title text-2xl font-semibold">Harnesses</h1>
            <HostSelect hosts={sortedHosts} selected={host} onSelect={setSelectedHostId} />
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
          {!host && <NoHostNotice />}
          {filtered.length === 0 && (
            <p className="text-ui text-muted-foreground">
              {query.trim() ? `No harnesses match “${query}”.` : "No installed harnesses."}
            </p>
          )}
          <div
            className={cn(
              "grid grid-cols-1 gap-3 @[520px]:grid-cols-2",
              // No host to resolve status against — dim the catalog to read as inactive.
              !host && "opacity-50",
            )}
          >
            {filtered.map((entry) => (
              <HarnessCard
                key={entry.harness}
                entry={entry}
                host={host}
                canSetup={canSetup}
                onSetup={() => host && setSetupTarget({ entry, host })}
              />
            ))}
          </div>
        </>
      )}
      <HarnessSetupDialog
        open={setupTarget !== null}
        onOpenChange={(open) => !open && setSetupTarget(null)}
        agentName={setupTarget?.entry.name}
        harness={setupTarget?.entry.harness ?? null}
        host={setupTarget?.host ?? null}
      />
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
    reason: harnessUnavailableReasonOnHost(harness, host),
  };
}

function HarnessStatusText({ status }: { status: ReturnType<typeof harnessStatus> }) {
  if (status.ready) {
    return <span className="text-xs text-green-600 dark:text-green-400">Configured</span>;
  }
  if (status.needsSetup) {
    // Sentence case to match "Configured"; the picker keeps the shared lowercase badge.
    const text = harnessWarningBadgeText(status.reason);
    return (
      <span className="text-xs text-amber-600 dark:text-amber-500">
        {text.charAt(0).toUpperCase() + text.slice(1)}
      </span>
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
      onClick={onSetup}
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
  onSetup,
}: {
  entry: HarnessEntry;
  host: Host | null;
  canSetup: boolean;
  onSetup: () => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const navigate = useNavigate();
  const body = (
    <>
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-ui font-medium text-foreground">{entry.name}</span>
            <HarnessStatusText status={status} />
          </div>
        </div>
        {status.ready && (
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
        {status.needsSetup && canSetup && <SetupButton entry={entry} onSetup={onSetup} />}
      </div>
      <p className="line-clamp-2 text-ui text-muted-foreground">{entry.description}</p>
    </>
  );
  const className =
    "flex flex-col gap-2 rounded-[20px] border border-border bg-card p-4 transition-colors";
  return status.ready ? (
    <Link
      to={`/settings/harnesses/${entry.harness}`}
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
  onSetup,
}: {
  entry: HarnessEntry;
  host: Host | null;
  canSetup: boolean;
  onSetup: () => void;
}) {
  const status = harnessStatus(entry.harness, host);
  const header = (
    <>
      <BackButton label="Harnesses" to="/settings/harnesses" />
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 items-center gap-3">
          <HarnessIcon entry={entry} />
          <div className="flex min-w-0 flex-col">
            <h1 className="settings-page-title truncate text-2xl font-semibold">{entry.name}</h1>
            <HarnessStatusText status={status} />
          </div>
        </div>
        {status.needsSetup && canSetup && <SetupButton entry={entry} onSetup={onSetup} />}
      </div>
    </>
  );
  const credential = <CredentialCard gateway={host?.gateway_inference?.[entry.harness] === true} />;
  // The host inventory only covers these families' MCP servers, skills, and plugins.
  const family = BRAND_HARNESSES.find((f) => INVENTORY_HARNESS_IDS[f] === entry.harness);
  if (status.ready && host && family) {
    return (
      <HarnessCatalog
        header={header}
        settings={
          <div className="flex flex-col gap-6">
            {credential}
            <StartupSettings host={host} harness={entry.harness} />
          </div>
        }
        host={host}
        family={family}
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
            {credential}
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
function CredentialCard({ gateway }: { gateway: boolean }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        Credential
      </h2>
      <div className="flex items-center gap-3 rounded-xl border border-border p-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted">
          <KeyRoundIcon className="size-4 text-muted-foreground" />
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="text-ui font-medium">{gateway ? "AI Gateway" : "Signed in"}</span>
          <span className="text-xs text-muted-foreground">
            {gateway
              ? "Managed credential via the Databricks AI Gateway"
              : "Uses the harness's own login on this host"}
          </span>
        </span>
      </div>
    </section>
  );
}
