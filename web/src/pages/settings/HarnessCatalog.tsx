import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import * as AccordionPrimitive from "radix-ui/accordion";
import { ArrowLeftIcon, ChevronRightIcon, PlugIcon, SparkleIcon } from "lucide-react";
import { Link, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { BrandHarness } from "@/components/onboarding/harnessBrand";
import type { Host } from "@/hooks/useHosts";
import {
  type InventoryMcpServer,
  type InventoryPlugin,
  type InventorySkill,
  useHarnessInventory,
} from "@/hooks/useHarnessInventory";
import { type CatalogKind, MOCK_SKILL_CONTENT, mockTools } from "./harnessCatalogMocks";

const KINDS: { id: CatalogKind; label: string; noun: string }[] = [
  { id: "mcps", label: "MCP servers", noun: "MCP servers" },
  { id: "skills", label: "Skills", noun: "skills" },
  { id: "plugins", label: "Plugins", noun: "plugins" },
];

type OpenItem =
  | { kind: "skills"; item: InventorySkill }
  | { kind: "plugins"; item: InventoryPlugin; mcps: InventoryMcpServer[] };

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
 * Settings tab showing `settings`. Opening a row replaces the page (header
 * included) with that item's details; Back returns to the same tab.
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
  const [open, setOpen] = useState<OpenItem | null>(null);
  const inventory = useHarnessInventory(host);
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
    own.mcps.filter((server) => server.plugin === plugin.name);
  const loading = inventory.status === "loading";
  // Plugins come from both the skill and MCP listings, so they fail only with both.
  const failed = (kind: CatalogKind) =>
    kind === "plugins" ? unavailable.length === 2 : unavailable.includes(kind);

  if (open?.kind === "skills") return <SkillPage skill={open.item} onBack={back} />;
  if (open?.kind === "plugins") {
    return <PluginPage plugin={open.item} mcps={open.mcps} onBack={back} />;
  }

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
            // Radix primitives directly: the shared Accordion puts the chevron on
            // the right and animates; this list wants it leading and instant.
            <AccordionPrimitive.Root
              type="multiple"
              className="flex flex-col rounded-xl border border-border"
            >
              {own.mcps.map((server) => {
                const tools = mockTools(server.name);
                return (
                  <AccordionPrimitive.Item
                    key={server.id}
                    value={server.id}
                    className="not-last:border-b"
                  >
                    <AccordionPrimitive.Header className="flex">
                      <AccordionPrimitive.Trigger
                        className="group flex min-w-0 flex-1 cursor-pointer items-center gap-2 px-4 py-2.5 text-left text-ui outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50"
                        data-testid={`catalog-row-${server.name}`}
                      >
                        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground group-aria-expanded:rotate-90" />
                        <LetterAvatar name={server.name} />
                        <span className="shrink-0 font-medium text-foreground">{server.name}</span>
                        <span className="shrink-0 text-muted-foreground">
                          · {plural(tools.length, "tool")}
                        </span>
                        {server.detail && (
                          <span className="min-w-0 truncate text-muted-foreground">
                            {server.detail}
                          </span>
                        )}
                      </AccordionPrimitive.Trigger>
                    </AccordionPrimitive.Header>
                    <AccordionPrimitive.Content className="pr-4 pb-2.5 pl-10">
                      <ul className="flex flex-col gap-1 font-mono text-xs text-muted-foreground">
                        {tools.map((tool) => (
                          <li key={tool}>{tool}</li>
                        ))}
                      </ul>
                    </AccordionPrimitive.Content>
                  </AccordionPrimitive.Item>
                );
              })}
            </AccordionPrimitive.Root>,
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
                  onOpen={() => setOpen({ kind: "skills", item: skill })}
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
                    detail={`${plural(plugin.skills.length, "skill")} · ${plural(mcps.length, "MCP")}`}
                    onOpen={() => setOpen({ kind: "plugins", item: plugin, mcps })}
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
        <div className={mainClass}>{main}</div>
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

function SkillPage({ skill, onBack }: { skill: InventorySkill; onBack: () => void }) {
  return (
    <>
      <BackButton label="Skills" onClick={onBack} />
      <h1 className="truncate text-2xl font-semibold">{skill.name}</h1>
      {skill.description && (
        <>
          <h2 className="mt-6 text-ui font-medium">Description</h2>
          <p className="mt-1 text-ui text-muted-foreground">{skill.description}</p>
        </>
      )}
      <h2 className="mt-6 text-ui font-medium">Contents</h2>
      <div className="prose prose-sm mt-2 max-w-none rounded-xl border border-border p-5 dark:prose-invert">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{MOCK_SKILL_CONTENT}</ReactMarkdown>
      </div>
    </>
  );
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
          <span className="text-ui text-muted-foreground">
            {plural(plugin.skills.length, "skill")} · {plural(mcps.length, "MCP")}
          </span>
        </div>
      </div>
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
