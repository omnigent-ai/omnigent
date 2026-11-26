import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSetMcpServerEnabled } from "@/hooks/useSetMcpServerEnabled";
import * as AccordionPrimitive from "radix-ui/accordion";
import { useMcpServerTools, type McpServerTools } from "@/hooks/useMcpServerTools";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeftIcon, ChevronRightIcon, PlugIcon, SparkleIcon } from "lucide-react";
import { Link, useLocation, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { mcpServerDisplayName } from "@/lib/mcpServerName";
import { Button } from "@/components/ui/button";
import {
  HARNESS_DETAIL_ENTRIES,
  HARNESS_PLUGIN_ENTRIES,
  HARNESS_SKILL_ENTRIES,
  type InventoryPageEntries,
  type HarnessPluginEntry,
  type HarnessSkillEntry,
  INVENTORY_COUNT_SEPARATOR,
  INVENTORY_KIND_LABELS,
  type InventoryPageHarnessId,
  type McpServerListEntry,
  isSkillPageHarness,
  MCP_SERVER_LIST_ENTRIES,
  SKILL_PAGE_FAMILIES,
  type SkillPageHarnessId,
} from "./search/catalog/harnesses";
import { settingsEntryById } from "./search/catalog";
import { settingsRevealer } from "./search/catalog/types";
import { parseSettingsHash, settingsControlProps } from "./search/settingsControl";
import { SettingsItem } from "./search/SettingsItem";
import {
  SettingsContent,
  SettingsDataRegion,
  SettingsOwner,
  useSettingsOwnership,
} from "./search/settingsOwnership";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Host } from "@/hooks/useHosts";
import {
  type InventoryHarness,
  type InventoryMcpServer,
  type InventoryPlugin,
  useHarnessInventory,
} from "@/hooks/useHarnessInventory";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSkillContent } from "@/hooks/useSkillContent";
import { ApiError } from "@/lib/sessionsApi";
import { Spinner } from "../../components/ui/spinner";

type CatalogKind = "mcps" | "skills" | "plugins";

// The server probes MCP tools and reads skill contents for these families only.
const DETAILED_HARNESSES: ReadonlySet<InventoryHarness> = new Set<InventoryHarness>(
  SKILL_PAGE_FAMILIES,
);

const KINDS: { id: CatalogKind; label: string; noun: string }[] = [
  { id: "mcps", label: INVENTORY_KIND_LABELS.mcps, noun: "MCP servers" },
  { id: "skills", label: INVENTORY_KIND_LABELS.skills, noun: "skills" },
  { id: "plugins", label: INVENTORY_KIND_LABELS.plugins, noun: "plugins" },
];

/** The tab the URL's row is revealed through on this page, as a search result's MCP list is. */
function revealedTab(hash: string, detailEntryId: string): CatalogKind | undefined {
  const target = parseSettingsHash(hash);
  const entry = target === null ? undefined : settingsEntryById(target.id);
  const revealer = entry === undefined ? undefined : settingsRevealer(entry);
  if (revealer?.id !== detailEntryId || revealer.part !== "tab") return undefined;
  return KINDS.find((kind) => kind.label === revealer.choice)?.id;
}

/** "← label" row above a page title: a link when `to` is set, else a button. */
export function BackButton({
  label,
  to,
  onClick,
  componentId = "settings.harnesses.back",
}: {
  label: string;
  to?: string;
  onClick?: () => void;
  componentId?: string;
}) {
  // Inside a Settings owner, named and found as its part, around its label where the part is
  // named by a template; elsewhere, by its visible label.
  const owned = useSettingsOwnership();
  const className = "mb-4 -ml-2.5 font-normal";
  // A link renders as the button when it goes somewhere; otherwise the button acts itself.
  return to !== undefined ? (
    <Button asChild variant="ghost" size="sm" className={className} onClick={onClick}>
      <Link
        to={to}
        componentId={componentId}
        {...owned.control({ name: { runtime: label }, outside: {} })}
      >
        <ArrowLeftIcon />
        {label}
      </Link>
    </Button>
  ) : (
    <Button
      variant="ghost"
      size="sm"
      className={className}
      onClick={onClick}
      {...owned.control({ name: { runtime: label }, outside: {} })}
    >
      <ArrowLeftIcon />
      {label}
    </Button>
  );
}

/**
 * MCP servers / Skills / Plugins tabs of a harness, listed from the host, plus a
 * Settings tab showing `settings`. Opening a plugin replaces the page (header
 * included) with its skills and MCP servers; Back returns to the Plugins tab.
 */
export function HarnessCatalog({
  header,
  settings,
  host,
  entries,
  defaultTab = "mcps",
}: {
  header: ReactNode;
  settings: ReactNode;
  host: Host;
  entries: InventoryPageEntries;
  defaultTab?: CatalogKind | "settings";
}) {
  const family = entries.family;
  // `?tab=settings` (the grid card's gear) opens on Settings, and a revealed row on its own tab;
  // otherwise the page's default.
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const detailEntryId = HARNESS_DETAIL_ENTRIES.entry(entries.harness).id;
  const [tab, setTab] = useState<CatalogKind | "settings">(() =>
    searchParams.get("tab") === "settings"
      ? "settings"
      : (revealedTab(location.hash, detailEntryId) ?? defaultTab),
  );
  const [open, setOpen] = useState<InventoryPlugin | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<{ name: string; sourceId?: string } | null>(
    null,
  );
  // A later navigation to a row on one of the tabs, such as a search result picked while this
  // page is open, shows that tab; clicking a tab doesn't navigate, so it's left alone.
  const handledNavigation = useRef(location.key);
  const pageOpen = selectedSkill !== null ? entries.skill?.id : open ? entries.plugin.id : null;
  useEffect(() => {
    if (location.key === handledNavigation.current) return;
    handledNavigation.current = location.key;
    const kind = revealedTab(location.hash, detailEntryId);
    // The open plugin or skill page is the row itself, which reveals itself.
    if (kind === undefined || parseSettingsHash(location.hash)?.id === pageOpen) return;
    setSelectedSkill(null);
    setOpen(null);
    setTab(kind);
  }, [location.key, location.hash, detailEntryId, pageOpen]);
  const [contentSupported, setContentSupported] = useState(DETAILED_HARNESSES.has(family));
  const families = useMemo(() => [family], [family]);
  const inventory = useHarnessInventory(host, {
    includePluginMetadata: family === "claude",
    families,
  });
  const back = () => setOpen(null);

  const { context, unavailable } = inventory;
  const mine = <T extends { harness: InventoryHarness }>(items: T[]) =>
    items.filter((item) => item.harness === family);
  const own = {
    mcps: mine(context.mcps),
    skills: mine(context.skills),
    plugins: mine(context.plugins),
  };
  const pluginMcps = (plugin: InventoryPlugin): InventoryMcpServer[] => {
    const enablement = (name: string, sourceId?: string) => {
      const matches = own.mcps.filter(
        (server) =>
          server.plugin === plugin.name &&
          server.name === name &&
          (sourceId === undefined || server.sourceId === sourceId),
      );
      const match = matches.length === 1 ? matches[0] : undefined;
      return { enabled: match?.enabled, locked: match?.locked };
    };
    return (
      plugin.mcp_entries?.map(({ id, name }) => ({
        id,
        name,
        sourceId: id,
        harness: family,
        plugin: plugin.name,
        ...enablement(name, id),
      })) ??
      plugin.mcp_servers?.map((name) => ({
        id: `${plugin.id}:${name}`,
        name,
        harness: family,
        plugin: plugin.name,
        ...enablement(name),
      })) ??
      own.mcps.filter((server) => server.plugin === plugin.name)
    );
  };
  const loading = inventory.status === "loading";
  const failed = (kind: CatalogKind) =>
    kind === "plugins" && family !== "claude"
      ? unavailable.includes("skills") && unavailable.includes("mcps")
      : unavailable.includes(kind);

  if (selectedSkill !== null && entries.skill && isSkillPageHarness(entries.harness))
    return (
      <SkillPage
        host={host}
        entry={entries.skill}
        harness={entries.harness}
        name={selectedSkill.name}
        sourceId={selectedSkill.sourceId}
        backLabel={open ? open.name : "Skills"}
        onBack={() => setSelectedSkill(null)}
        onUnavailable={() => {
          setContentSupported(false);
          setSelectedSkill(null);
        }}
      />
    );
  if (open)
    return (
      <PluginPage
        host={host}
        entry={entries.plugin}
        harness={entries.harness}
        plugin={open}
        mcps={pluginMcps(open)}
        onBack={back}
        onSkillOpen={
          contentSupported ? (name, sourceId) => setSelectedSkill({ name, sourceId }) : undefined
        }
      />
    );

  const ownList = (kind: CatalogKind, count: number, list: ReactNode) => {
    const noun = KINDS.find((k) => k.id === kind)?.noun;
    if (loading) return <Notice>Loading {noun}…</Notice>;
    if (kind === "mcps" && inventory.mcpUnsupported)
      return <Notice>Please update host {host.name} to list MCP servers.</Notice>;
    if (failed(kind))
      return (
        <Notice>
          Couldn't load {noun} from {host.name}.
        </Notice>
      );
    if (count === 0)
      return (
        <Notice>
          No {noun} found on {host.name}.
        </Notice>
      );
    return list;
  };

  return (
    <>
      {header}
      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as CatalogKind | "settings")}
        componentId="settings.harnesses.tab"
        className="mt-8 gap-4"
      >
        <TabsList
          variant="line"
          className="w-full justify-start border-b border-border px-0"
          {...settingsControlProps(HARNESS_DETAIL_ENTRIES.entry(entries.harness), "tab")}
        >
          {KINDS.map((k) => (
            <TabsTrigger
              key={k.id}
              value={k.id}
              className="flex-none"
              data-testid={`harness-tab-${k.id}`}
            >
              {loading ? k.label : `${k.label}${INVENTORY_COUNT_SEPARATOR}${own[k.id].length}`}
            </TabsTrigger>
          ))}
          <TabsTrigger value="settings" className="flex-none" data-testid="harness-tab-settings">
            Settings
          </TabsTrigger>
        </TabsList>
        <TabsContent value="settings">{settings}</TabsContent>
        <TabsContent value="mcps">
          {ownList(
            "mcps",
            own.mcps.length,
            <McpList host={host} harness={entries.harness} servers={own.mcps} />,
          )}
        </TabsContent>
        <TabsContent value="skills">
          {ownList(
            "skills",
            own.skills.length,
            <SettingsDataRegion
              entry={HARNESS_DETAIL_ENTRIES.entry(entries.harness)}
              region="skills"
            >
              <ul className="flex flex-col gap-2">
                {own.skills.map((skill) => (
                  <CatalogRow
                    key={skill.id}
                    icon={<SparkleIcon className="size-4 text-muted-foreground" />}
                    name={skill.name}
                    detail={skill.description}
                    onOpen={
                      contentSupported ? () => setSelectedSkill({ name: skill.name }) : undefined
                    }
                  />
                ))}
              </ul>
            </SettingsDataRegion>,
          )}
        </TabsContent>
        <TabsContent value="plugins">
          {ownList(
            "plugins",
            own.plugins.length,
            <SettingsDataRegion
              entry={HARNESS_DETAIL_ENTRIES.entry(entries.harness)}
              region="plugins"
            >
              <ul className="flex flex-col gap-2">
                {own.plugins.map((plugin) => {
                  const mcps = pluginMcps(plugin);
                  return (
                    <CatalogRow
                      key={plugin.id}
                      icon={<PlugIcon className="size-4 text-muted-foreground" />}
                      name={plugin.name}
                      detail={[pluginDetail(plugin, mcps.length), plugin.description]
                        .filter(Boolean)
                        .join(" · ")}
                      onOpen={() => setOpen(plugin)}
                    />
                  );
                })}
              </ul>
            </SettingsDataRegion>,
          )}
        </TabsContent>
      </Tabs>
    </>
  );
}

function Notice({ children }: { children: ReactNode }) {
  return <p className="text-ui text-muted-foreground">{children}</p>;
}

/** "1 tool", "3 tools". */
function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** One bordered list row; it opens the item when `onOpen` is set. */
function CatalogRow({
  icon,
  name,
  label = name,
  detail,
  onOpen,
  trailing,
  dimmed,
}: {
  icon: ReactNode;
  name: string;
  /** Text shown for the row when it differs from its identifying name. */
  label?: string;
  detail?: string;
  onOpen?: () => void;
  trailing?: ReactNode;
  dimmed?: boolean;
}) {
  const main = (
    <>
      <span className="flex shrink-0 items-center">{icon}</span>
      <span className="shrink-0 text-ui font-medium text-foreground">{label}</span>
      {detail && <span className="min-w-0 truncate text-sm text-muted-foreground">{detail}</span>}
    </>
  );
  const mainClass = cn(
    "flex min-w-0 flex-1 items-center gap-2 px-4 py-2.5 text-left",
    dimmed && "opacity-50",
  );
  return (
    <li
      data-settings-region-row={onOpen ? true : undefined}
      className={cn(
        "flex items-center rounded-xl border border-border transition-colors",
        onOpen && "hover:bg-muted/50",
      )}
    >
      {onOpen ? (
        <button
          type="button"
          data-settings-row-action="open"
          onClick={onOpen}
          className={cn(mainClass, "cursor-pointer")}
          data-testid={`catalog-row-${name}`}
        >
          {main}
        </button>
      ) : (
        <div className={mainClass} data-testid={`catalog-row-${name}`}>
          {main}
        </div>
      )}
      {trailing}
    </li>
  );
}

function LetterAvatar({ name }: { name: string }) {
  return (
    <span
      aria-hidden
      className="flex size-6 items-center justify-center rounded-md border border-border text-xs text-muted-foreground uppercase"
    >
      {name[0]}
    </span>
  );
}

function pluginDetail(plugin: InventoryPlugin, mcpCount: number) {
  return [
    plugin.version && `v${plugin.version}`,
    plugin.marketplace,
    plugin.enabled !== undefined && (plugin.enabled ? "Enabled" : "Disabled"),
    plural(plugin.skills.length, "skill"),
    plural(mcpCount, "MCP"),
    plugin.has_hooks && "Hooks",
    plugin.has_commands && "Commands",
  ]
    .filter(Boolean)
    .join(" · ");
}

function PluginPage({
  host,
  entry,
  harness,
  plugin,
  mcps,
  onBack,
  onSkillOpen,
}: {
  host: Host;
  entry: HarnessPluginEntry;
  harness: InventoryPageHarnessId;
  plugin: InventoryPlugin;
  mcps: InventoryMcpServer[];
  onBack: () => void;
  onSkillOpen?: (name: string, sourceId?: string) => void;
}) {
  const needsUpdate = plugin.marketplace !== undefined && !plugin.skill_entries;
  const skills = plugin.skill_entries ?? plugin.skills.map((name) => ({ name, id: undefined }));
  return (
    <SettingsItem entry={entry}>
      <SettingsOwner entry={HARNESS_PLUGIN_ENTRIES.entry(harness)} part="back">
        <BackButton label="Plugins" onClick={onBack} />
      </SettingsOwner>
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border">
          <PlugIcon className="size-5 text-muted-foreground" />
        </span>
        <div className="flex min-w-0 flex-col">
          <h1 className="truncate text-2xl font-medium">{plugin.name}</h1>
          <span className="text-sm text-muted-foreground">{pluginDetail(plugin, mcps.length)}</span>
        </div>
      </div>
      {plugin.description && (
        <p className="mt-4 text-sm text-muted-foreground">{plugin.description}</p>
      )}
      <Tabs defaultValue="skills" className="mt-6 gap-4">
        <TabsList
          variant="line"
          className="w-full justify-start border-b border-border pb-1"
          {...settingsControlProps(HARNESS_PLUGIN_ENTRIES.entry(harness), "tab")}
        >
          <TabsTrigger value="skills" className="flex-none">
            Skills · {plugin.skills.length}
          </TabsTrigger>
          <TabsTrigger value="mcps" className="flex-none">
            MCPs · {mcps.length}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="skills">
          {needsUpdate && <Notice>Update {host.name} to read installed plugin skills.</Notice>}
          <SettingsDataRegion entry={entry} region="skills">
            <ul className="flex flex-col gap-2">
              {plugin.skills.length === 0 && <Notice>No skills found.</Notice>}
              {skills.map(({ name, id }) => (
                <CatalogRow
                  key={id ?? name}
                  icon={<SparkleIcon className="size-4 text-muted-foreground" />}
                  name={name}
                  onOpen={
                    onSkillOpen && !needsUpdate
                      ? () => onSkillOpen(`${plugin.name}:${name}`, id)
                      : undefined
                  }
                />
              ))}
            </ul>
          </SettingsDataRegion>
        </TabsContent>
        <TabsContent value="mcps">
          <McpList
            host={host}
            harness={harness}
            servers={mcps}
            unavailableReason={
              plugin.enabled === false
                ? "This plugin is disabled. Enable it in Claude Code to inspect MCP tools."
                : plugin.marketplace !== undefined && !plugin.mcp_entries
                  ? `Update ${host.name} to inspect installed plugin MCP tools.`
                  : undefined
            }
          />
        </TabsContent>
      </Tabs>
    </SettingsItem>
  );
}

function SkillPage({
  host,
  entry,
  harness,
  name,
  sourceId,
  backLabel,
  onBack,
  onUnavailable,
}: {
  host: Host;
  entry: HarnessSkillEntry;
  harness: SkillPageHarnessId;
  name: string;
  sourceId?: string;
  backLabel: string;
  onBack: () => void;
  onUnavailable: () => void;
}) {
  const query = useSkillContent(host.host_id, harness, name, { enabled: true, sourceId });
  const missingRoute = query.error instanceof ApiError && query.error.status === 404;
  useEffect(() => {
    if (missingRoute) onUnavailable();
  }, [missingRoute, onUnavailable]);
  return (
    <SettingsItem entry={entry}>
      <SettingsOwner entry={HARNESS_SKILL_ENTRIES.entry(harness)} part="back">
        <BackButton label={backLabel} onClick={onBack} />
      </SettingsOwner>
      <h1 className="truncate text-2xl font-medium">{name}</h1>
      {query.isPending ? (
        <Notice>Loading skill contents…</Notice>
      ) : query.error ? (
        <Notice>
          {query.error instanceof ApiError && query.error.status === 501
            ? `Update ${host.name} to see skill contents.`
            : "Couldn't load skill contents."}
        </Notice>
      ) : query.data ? (
        <>
          {query.data.description && (
            <p className="mt-4 text-sm text-muted-foreground">{query.data.description}</p>
          )}
          <h2 className="mt-6 text-ui font-medium">Contents</h2>
          {query.data.truncated && <Notice>Contents truncated (256 KiB limit).</Notice>}
          <SettingsContent entry={entry} area="skill">
            <div className="prose prose-sm mt-2 max-w-none overflow-x-auto rounded-xl border border-border p-5 dark:prose-invert">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                components={SKILL_MARKDOWN_COMPONENTS}
              >
                {query.data.content}
              </ReactMarkdown>
            </div>
          </SettingsContent>
        </>
      ) : null}
    </SettingsItem>
  );
}

const SKILL_MARKDOWN_COMPONENTS: Components = {
  img: ({ alt }) => <span>{alt}</span>,
  // Only SKILL.md is shown, so relative links (sibling files, anchors) lead nowhere.
  a: ({ href, children }) =>
    href && /^[a-z][a-z\d+.-]*:/i.test(href) ? (
      <a href={href} rel="noreferrer" target="_blank">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
};

function McpList({
  host,
  harness,
  servers,
  unavailableReason,
}: {
  host: Host;
  /** The harness whose MCP list this is, which names its rows' switches and opens. */
  harness: InventoryPageHarnessId;
  servers: InventoryMcpServer[];
  unavailableReason?: string;
}) {
  const entry = MCP_SERVER_LIST_ENTRIES.entry(harness) as McpServerListEntry;
  const [expanded, setExpanded] = useState<string[]>([]);
  const [supported, setSupported] = useState(true);
  if (servers.length === 0) return <Notice>No MCPs found.</Notice>;
  const probed = servers.every((server) => DETAILED_HARNESSES.has(server.harness));
  if (!supported || unavailableReason || !probed)
    return (
      <>
        {unavailableReason && <Notice>{unavailableReason}</Notice>}
        <SettingsDataRegion entry={MCP_SERVER_LIST_ENTRIES.entry(harness)} region="listed">
          <ul className="flex flex-col gap-2">
            {servers.map((server) => (
              <McpFallbackRow key={server.id} host={host} entry={entry} server={server} />
            ))}
          </ul>
        </SettingsDataRegion>
      </>
    );
  // Leading chevron and instant expansion match the harness catalog.
  return (
    <AccordionPrimitive.Root
      type="multiple"
      value={expanded}
      onValueChange={setExpanded}
      className="flex flex-col rounded-xl border border-border"
    >
      <SettingsDataRegion
        entry={MCP_SERVER_LIST_ENTRIES.entry(harness)}
        region="servers"
        className="contents"
      >
        {servers.map((server) => (
          <div key={server.id} data-settings-region-row className="not-last:border-b">
            <McpRow
              host={host}
              harness={harness}
              entry={entry}
              server={server}
              expanded={expanded.includes(server.id)}
              onUnavailable={() => setSupported(false)}
              onDisabled={() => setExpanded((current) => current.filter((id) => id !== server.id))}
            />
          </div>
        ))}
      </SettingsDataRegion>
    </AccordionPrimitive.Root>
  );
}

function McpSwitch({
  entry,
  server,
  enabled,
  mutation,
}: {
  entry: McpServerListEntry;
  server: InventoryMcpServer;
  enabled: boolean | null | undefined;
  mutation: ReturnType<typeof useSetMcpServerEnabled>;
}) {
  const harness = server.harness;
  if ((harness !== "claude" && harness !== "codex") || enabled === null || enabled === undefined)
    return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="flex items-center px-4">
          <Switch
            {...settingsControlProps(entry, "enabled", {
              runtime: mcpServerDisplayName(server.name),
            })}
            checked={enabled}
            disabled={server.locked || mutation.isPending}
            onCheckedChange={(checked) =>
              mutation.mutate({
                harness,
                server: server.name,
                plugin: server.plugin,
                source_id: server.sourceId,
                enabled: checked,
              })
            }
          />
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {server.locked ? "Managed by your organization" : "Omnigent sessions only"}
      </TooltipContent>
    </Tooltip>
  );
}

function useMcpEnabledState(hostId: string, server: InventoryMcpServer) {
  const mutation = useSetMcpServerEnabled(hostId);
  // Keep this row pending even when another row settles and refetches inventory.
  const enabled = mutation.isPending ? mutation.variables.enabled : server.enabled;
  return { mutation, enabled };
}

function McpFallbackRow({
  host,
  entry,
  server,
}: {
  host: Host;
  entry: McpServerListEntry;
  server: InventoryMcpServer;
}) {
  const { mutation, enabled } = useMcpEnabledState(host.host_id, server);
  return (
    <CatalogRow
      icon={<LetterAvatar name={server.name} />}
      name={server.name}
      label={mcpServerDisplayName(server.name)}
      detail={[server.detail, enabled === false && "Off"].filter(Boolean).join(" · ")}
      dimmed={enabled === false}
      trailing={<McpSwitch entry={entry} server={server} enabled={enabled} mutation={mutation} />}
    />
  );
}

const CONNECTION_DETAILS: Record<McpServerTools["connection"], string> = {
  connected: "Connected",
  needs_auth:
    "Authentication required. Harness sign-in credentials cannot be reused for this probe.",
  unreachable: "Couldn't reach this MCP server.",
  timeout: "MCP probe timed out.",
  unsupported: "This MCP configuration cannot be probed from the host.",
};

const CONNECTION_STATUS: Record<McpServerTools["connection"], { label: string; color: string }> = {
  connected: { label: "Connected", color: "bg-success" },
  needs_auth: { label: "Needs auth", color: "bg-warning" },
  unreachable: { label: "Failed to connect", color: "bg-destructive" },
  timeout: { label: "Failed to connect", color: "bg-destructive" },
  unsupported: { label: "Failed to connect", color: "bg-destructive" },
};

function McpRow({
  host,
  harness,
  entry,
  server,
  expanded,
  onUnavailable,
  onDisabled,
}: {
  host: Host;
  /** The harness whose list this row is in, whose tools region it holds. */
  harness: InventoryPageHarnessId;
  entry: McpServerListEntry;
  server: InventoryMcpServer;
  expanded: boolean;
  onUnavailable: () => void;
  onDisabled: () => void;
}) {
  const { mutation, enabled } = useMcpEnabledState(host.host_id, server);
  const off = enabled === false;
  useEffect(() => {
    if (off && expanded) onDisabled();
  }, [off, expanded, onDisabled]);
  const query = useMcpServerTools(host.host_id, server.harness, server.name, server.plugin, {
    enabled: expanded && !off,
    sourceId: server.sourceId,
  });
  const missingRoute = query.error instanceof ApiError && query.error.status === 404;
  useEffect(() => {
    if (missingRoute) onUnavailable();
  }, [missingRoute, onUnavailable]);
  const data = query.data;
  const status = data && CONNECTION_STATUS[data.connection];
  return (
    <AccordionPrimitive.Item value={server.id} disabled={off}>
      <AccordionPrimitive.Header className="flex">
        <AccordionPrimitive.Trigger
          data-settings-row-action="open"
          className={cn(
            "group flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-4 py-2.5 text-left text-ui outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50",
            off && "opacity-50",
          )}
          data-testid={`catalog-row-${server.name}`}
        >
          <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground group-aria-expanded:rotate-90" />
          <LetterAvatar name={server.name} />
          <span className="shrink-0 font-medium text-foreground">
            {mcpServerDisplayName(server.name)}
          </span>
          {!off && data?.connection === "connected" && (
            <span className="shrink-0 text-muted-foreground">
              · {data.tools.length}
              {data.truncated ? "+" : ""} {data.tools.length === 1 ? "tool" : "tools"}
            </span>
          )}
          {server.detail && (
            <span className="min-w-0 truncate text-muted-foreground">{server.detail}</span>
          )}
          {off && <span className="ml-auto text-muted-foreground">Off</span>}
          {!off && status && (
            <span role="status" className="ml-auto flex shrink-0 items-center gap-1.5 text-xs">
              <span aria-hidden="true" className={cn("size-2 rounded-full", status.color)} />
              {status.label}
            </span>
          )}
        </AccordionPrimitive.Trigger>
        <McpSwitch entry={entry} server={server} enabled={enabled} mutation={mutation} />
      </AccordionPrimitive.Header>
      {!off && (
        <AccordionPrimitive.Content className="pr-4 pb-2.5 pl-10">
          {query.isPending ? (
            <div className="flex items-center gap-2 mt-1">
              <Spinner />
              <Notice>Loading tools...</Notice>
            </div>
          ) : query.error ? (
            <Notice>
              {query.error instanceof ApiError && query.error.status === 501
                ? `Update ${host.name} to list tools.`
                : query.error instanceof ApiError && query.error.status === 503
                  ? "Host is busy probing other MCP servers. Collapse and reopen to retry."
                  : `Couldn't reach ${mcpServerDisplayName(server.name)}.`}
            </Notice>
          ) : data ? (
            data.connection !== "connected" ? (
              <Notice>{CONNECTION_DETAILS[data.connection]}</Notice>
            ) : (
              <>
                {data.tools.length === 0 && <Notice>No tools reported.</Notice>}
                {data.truncated && <Notice>Showing the first 500 tools.</Notice>}
                {/* Each tool is a row named by the tool; one with a description opens to it. */}
                <SettingsDataRegion entry={MCP_SERVER_LIST_ENTRIES.entry(harness)} region="tools">
                  <ul className="flex flex-col gap-1 text-xs text-muted-foreground my-2">
                    {data.tools.map((tool, index) => (
                      // Capped names can collide; these display-only rows have no state.
                      // eslint-disable-next-line react/no-array-index-key
                      <li key={`${tool.name}-${index}`} data-settings-region-row>
                        {tool.description ? (
                          <details className="group">
                            <summary
                              data-settings-row-action="expand"
                              className="flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden"
                            >
                              <span className="font-mono">{tool.name}</span>
                              <ChevronRightIcon className="size-3 shrink-0 group-open:rotate-90" />
                            </summary>
                            <div className="my-1">{tool.description}</div>
                          </details>
                        ) : (
                          <span className="pl-4 font-mono">{tool.name}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </SettingsDataRegion>
              </>
            )
          ) : null}
        </AccordionPrimitive.Content>
      )}
    </AccordionPrimitive.Item>
  );
}
