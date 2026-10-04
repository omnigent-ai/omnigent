import { useState, type ReactNode } from "react";
import { ArrowLeftIcon, PlugIcon, SparkleIcon } from "lucide-react";
import { Link, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { BrandHarness } from "@/components/onboarding/harnessBrand";
import type { Host } from "@/hooks/useHosts";
import {
  type InventoryMcpServer,
  type InventoryPlugin,
  useHarnessInventory,
} from "@/hooks/useHarnessInventory";

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
}: {
  label: string;
  to?: string;
  onClick?: () => void;
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
        <Link to={to} componentId="settings.harnesses.back">
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
  const inventory = useHarnessInventory(host, { includePluginMetadata: true });
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
    plugin.mcp_servers?.map((name) => ({
      id: `${plugin.id}:${name}`,
      name,
      harness: family,
      plugin: plugin.name,
    })) ?? own.mcps.filter((server) => server.plugin === plugin.name);
  const loading = inventory.status === "loading";
  const failed = (kind: CatalogKind) =>
    kind === "plugins" && family !== "claude"
      ? unavailable.includes("skills") && unavailable.includes("mcps")
      : unavailable.includes(kind);

  if (open) return <PluginPage plugin={open.plugin} mcps={open.mcps} onBack={back} />;

  const ownList = (kind: CatalogKind, count: number, list: ReactNode) => {
    const noun = KINDS.find((k) => k.id === kind)?.noun;
    if (loading) return <Notice>Loading {noun}…</Notice>;
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
          {ownList(
            "mcps",
            own.mcps.length,
            <ul className="flex flex-col gap-2">
              {own.mcps.map((server) => (
                <CatalogRow
                  key={server.id}
                  icon={<LetterAvatar name={server.name} />}
                  name={server.name}
                  detail={server.detail}
                />
              ))}
            </ul>,
          )}
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
    plugin.enabled === undefined ? undefined : plugin.enabled ? "Enabled" : "Disabled",
    plural(plugin.skills.length, "skill"),
    plural(mcpCount, "MCP"),
    plugin.has_hooks && "Hooks",
    plugin.has_commands && "Commands",
  ]
    .filter(Boolean)
    .join(" · ");
}

function PluginPage({
  plugin,
  mcps,
  onBack,
}: {
  plugin: InventoryPlugin;
  mcps: InventoryMcpServer[];
  onBack: () => void;
}) {
  return (
    <>
      <BackButton label="Plugins" onClick={onBack} />
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border">
          <PlugIcon className="size-5 text-muted-foreground" />
        </span>
        <div className="flex min-w-0 flex-col">
          <h1 className="truncate text-2xl font-semibold">{plugin.name}</h1>
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
          <ul className="flex flex-col gap-2">
            {plugin.skills.map((name) => (
              <CatalogRow
                key={name}
                icon={<SparkleIcon className="size-4 text-muted-foreground" />}
                name={name}
              />
            ))}
          </ul>
        </TabsContent>
        <TabsContent value="mcps">
          <ul className="flex flex-col gap-2">
            {mcps.map((server) => (
              <CatalogRow
                key={server.id}
                icon={<LetterAvatar name={server.name} />}
                name={server.name}
                detail={server.detail}
              />
            ))}
          </ul>
        </TabsContent>
      </Tabs>
    </>
  );
}
