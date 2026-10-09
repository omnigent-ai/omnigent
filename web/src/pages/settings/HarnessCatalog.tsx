import * as AccordionPrimitive from "radix-ui/accordion";
import { useMcpServerTools, type McpServerTools } from "@/hooks/useMcpServerTools";
import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeftIcon, ChevronRightIcon, PlugIcon, SparkleIcon } from "lucide-react";
import { Link, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { BrandHarness } from "@/components/onboarding/harnessBrand";
import type { Host } from "@/hooks/useHosts";
import {
  INVENTORY_HARNESS_IDS,
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

const KINDS: { id: CatalogKind; label: string; noun: string }[] = [
  { id: "mcps", label: "MCP servers", noun: "MCP servers" },
  { id: "skills", label: "Skills", noun: "skills" },
  { id: "plugins", label: "Plugins", noun: "plugins" },
];

interface OpenPlugin {
  plugin: InventoryPlugin;
  mcps: InventoryMcpServer[];
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
  return (
    <Button
      asChild={to !== undefined}
      variant="ghost"
      size="sm"
      className="mb-4 -ml-2.5 font-normal"
      onClick={onClick}
    >
      {to !== undefined ? (
        <Link to={to} componentId={componentId}>
          <ArrowLeftIcon />
          {label}
        </Link>
      ) : (
        <>
          <ArrowLeftIcon />
          {label}
        </>
      )}
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
  family,
}: {
  header: ReactNode;
  settings: ReactNode;
  host: Host;
  family: BrandHarness;
}) {
  // `?tab=settings` (the grid card's gear) opens on Settings; otherwise MCP servers.
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<CatalogKind | "settings">(
    searchParams.get("tab") === "settings" ? "settings" : "mcps",
  );
  const [open, setOpen] = useState<OpenPlugin | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<{ name: string; sourceId?: string } | null>(
    null,
  );
  const [contentSupported, setContentSupported] = useState(true);
  const inventory = useHarnessInventory(host, { includePluginMetadata: family === "claude" });
  const back = () => setOpen(null);

  const { context, unavailable } = inventory;
  const mine = <T extends { harness: BrandHarness }>(items: T[]) =>
    items.filter((item) => item.harness === family);
  const own = {
    mcps: mine(context.mcps),
    skills: mine(context.skills),
    plugins: mine(context.plugins),
  };
  const pluginMcps = (plugin: InventoryPlugin) =>
    plugin.mcp_entries?.map(({ id, name }) => ({
      id,
      name,
      sourceId: id,
      harness: family,
      plugin: plugin.name,
    })) ??
    plugin.mcp_servers?.map((name) => ({
      id: `${plugin.id}:${name}`,
      name,
      harness: family,
      plugin: plugin.name,
    })) ??
    own.mcps.filter((server) => server.plugin === plugin.name);
  const loading = inventory.status === "loading";
  const failed = (kind: CatalogKind) =>
    kind === "plugins" && family !== "claude"
      ? unavailable.includes("skills") && unavailable.includes("mcps")
      : unavailable.includes(kind);

  if (selectedSkill !== null)
    return (
      <SkillPage
        host={host}
        harness={INVENTORY_HARNESS_IDS[family]}
        name={selectedSkill.name}
        sourceId={selectedSkill.sourceId}
        backLabel={open ? open.plugin.name : "Skills"}
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
        plugin={open.plugin}
        mcps={open.mcps}
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
        <TabsList variant="line" className="w-full justify-start border-b border-border px-0">
          {KINDS.map((k) => (
            <TabsTrigger
              key={k.id}
              value={k.id}
              className="flex-none"
              data-testid={`harness-tab-${k.id}`}
            >
              {loading ? k.label : `${k.label} · ${own[k.id].length}`}
            </TabsTrigger>
          ))}
          <TabsTrigger value="settings" className="flex-none" data-testid="harness-tab-settings">
            Settings
          </TabsTrigger>
        </TabsList>
        <TabsContent value="settings">{settings}</TabsContent>
        <TabsContent value="mcps">
          {ownList("mcps", own.mcps.length, <McpList host={host} servers={own.mcps} />)}
        </TabsContent>
        <TabsContent value="skills">
          {ownList(
            "skills",
            own.skills.length,
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
            </ul>,
          )}
        </TabsContent>
        <TabsContent value="plugins">
          {ownList(
            "plugins",
            own.plugins.length,
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
                    onOpen={() => setOpen({ plugin, mcps })}
                  />
                );
              })}
            </ul>,
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
  detail,
  onOpen,
}: {
  icon: ReactNode;
  name: string;
  detail?: string;
  onOpen?: () => void;
}) {
  const main = (
    <>
      <span className="flex shrink-0 items-center">{icon}</span>
      <span className="shrink-0 text-ui font-medium text-foreground">{name}</span>
      {detail && <span className="min-w-0 truncate text-ui text-muted-foreground">{detail}</span>}
    </>
  );
  const mainClass = "flex min-w-0 flex-1 items-center gap-2 px-4 py-2.5 text-left";
  return (
    <li
      className={cn(
        "flex items-center rounded-xl border border-border transition-colors",
        onOpen && "hover:bg-muted/50",
      )}
    >
      {onOpen ? (
        <button
          type="button"
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
  plugin,
  mcps,
  onBack,
  onSkillOpen,
}: {
  host: Host;
  plugin: InventoryPlugin;
  mcps: InventoryMcpServer[];
  onBack: () => void;
  onSkillOpen?: (name: string, sourceId?: string) => void;
}) {
  const needsUpdate = plugin.marketplace !== undefined && !plugin.skill_entries;
  const skills = plugin.skill_entries ?? plugin.skills.map((name) => ({ name, id: undefined }));
  return (
    <>
      <BackButton label="Plugins" onClick={onBack} />
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border">
          <PlugIcon className="size-5 text-muted-foreground" />
        </span>
        <div className="flex min-w-0 flex-col">
          <h1 className="settings-page-title truncate text-2xl font-semibold">{plugin.name}</h1>
          <span className="text-ui text-muted-foreground">{pluginDetail(plugin, mcps.length)}</span>
        </div>
      </div>
      {plugin.description && (
        <p className="mt-4 text-ui text-muted-foreground">{plugin.description}</p>
      )}
      <Tabs defaultValue="skills" className="mt-6 gap-4">
        <TabsList variant="line" className="w-full justify-start border-b border-border pb-1">
          <TabsTrigger value="skills" className="flex-none">
            Skills · {plugin.skills.length}
          </TabsTrigger>
          <TabsTrigger value="mcps" className="flex-none">
            MCPs · {mcps.length}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="skills">
          {needsUpdate && <Notice>Update {host.name} to read installed plugin skills.</Notice>}
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
        </TabsContent>
        <TabsContent value="mcps">
          <McpList
            host={host}
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
    </>
  );
}

function SkillPage({
  host,
  harness,
  name,
  sourceId,
  backLabel,
  onBack,
  onUnavailable,
}: {
  host: Host;
  harness: string;
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
    <>
      <BackButton label={backLabel} onClick={onBack} />
      <h1 className="settings-page-title truncate text-2xl font-semibold">{name}</h1>
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
            <p className="mt-4 text-ui text-muted-foreground">{query.data.description}</p>
          )}
          <h2 className="mt-6 text-ui font-medium">Contents</h2>
          {query.data.truncated && <Notice>Contents truncated (256 KiB limit).</Notice>}
          <div className="prose prose-sm mt-2 max-w-none overflow-x-auto rounded-xl border border-border p-5 dark:prose-invert">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              skipHtml
              components={SKILL_MARKDOWN_COMPONENTS}
            >
              {query.data.content}
            </ReactMarkdown>
          </div>
        </>
      ) : null}
    </>
  );
}

const SKILL_MARKDOWN_COMPONENTS: Components = {
  img: ({ alt }) => <span>{alt}</span>,
  a: ({ href, children }) => (
    <a href={href} rel="noreferrer" target="_blank">
      {children}
    </a>
  ),
};

function McpList({
  host,
  servers,
  unavailableReason,
}: {
  host: Host;
  servers: InventoryMcpServer[];
  unavailableReason?: string;
}) {
  const [expanded, setExpanded] = useState<string[]>([]);
  const [supported, setSupported] = useState(true);
  if (servers.length === 0) return <Notice>No MCPs found.</Notice>;
  if (!supported || unavailableReason)
    return (
      <>
        {unavailableReason && <Notice>{unavailableReason}</Notice>}
        <ul className="flex flex-col gap-2">
          {servers.map((server) => (
            <CatalogRow
              key={server.id}
              icon={<LetterAvatar name={server.name} />}
              name={server.name}
              detail={server.detail}
            />
          ))}
        </ul>
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
      {servers.map((server) => (
        <McpRow
          key={server.id}
          host={host}
          server={server}
          expanded={expanded.includes(server.id)}
          onUnavailable={() => setSupported(false)}
        />
      ))}
    </AccordionPrimitive.Root>
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
  server,
  expanded,
  onUnavailable,
}: {
  host: Host;
  server: InventoryMcpServer;
  expanded: boolean;
  onUnavailable: () => void;
}) {
  const query = useMcpServerTools(host.host_id, server.harness, server.name, server.plugin, {
    enabled: expanded,
    sourceId: server.sourceId,
  });
  const missingRoute = query.error instanceof ApiError && query.error.status === 404;
  useEffect(() => {
    if (missingRoute) onUnavailable();
  }, [missingRoute, onUnavailable]);
  const data = query.data;
  const status = data && CONNECTION_STATUS[data.connection];
  return (
    <AccordionPrimitive.Item value={server.id} className="not-last:border-b">
      <AccordionPrimitive.Header className="flex">
        <AccordionPrimitive.Trigger
          className="group flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-4 py-2.5 text-left text-ui outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50"
          data-testid={`catalog-row-${server.name}`}
        >
          <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground group-aria-expanded:rotate-90" />
          <LetterAvatar name={server.name} />
          <span className="shrink-0 font-medium text-foreground">{server.name}</span>
          {data?.connection === "connected" && (
            <span className="shrink-0 text-muted-foreground">
              · {data.tools.length}
              {data.truncated ? "+" : ""} {data.tools.length === 1 ? "tool" : "tools"}
            </span>
          )}
          {server.detail && (
            <span className="min-w-0 truncate text-muted-foreground">{server.detail}</span>
          )}
          {status && (
            <span role="status" className="ml-auto flex shrink-0 items-center gap-1.5 text-xs">
              <span aria-hidden="true" className={cn("size-2 rounded-full", status.color)} />
              {status.label}
            </span>
          )}
        </AccordionPrimitive.Trigger>
      </AccordionPrimitive.Header>
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
                : `Couldn't reach ${server.name}.`}
          </Notice>
        ) : data ? (
          data.connection !== "connected" ? (
            <Notice>{CONNECTION_DETAILS[data.connection]}</Notice>
          ) : (
            <>
              {data.tools.length === 0 && <Notice>No tools reported.</Notice>}
              {data.truncated && <Notice>Showing the first 500 tools.</Notice>}
              <ul className="flex flex-col gap-1 text-xs text-muted-foreground my-2">
                {data.tools.map((tool, index) => (
                  // Capped names can collide; these display-only rows have no state.
                  // eslint-disable-next-line react/no-array-index-key
                  <li key={`${tool.name}-${index}`}>
                    {tool.description ? (
                      <details className="group">
                        <summary className="flex cursor-pointer list-none items-center gap-1 [&::-webkit-details-marker]:hidden">
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
            </>
          )
        ) : null}
      </AccordionPrimitive.Content>
    </AccordionPrimitive.Item>
  );
}
