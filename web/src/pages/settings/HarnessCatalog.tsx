import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeftIcon,
  ChevronDownIcon,
  EllipsisVerticalIcon,
  PlugIcon,
  PlusIcon,
  RefreshCwIcon,
  SparkleIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { BrandHarness } from "@/components/onboarding/harnessBrand";
import type { Host } from "@/hooks/useHosts";
import {
  type InventoryMcpServer,
  type InventoryPlugin,
  type InventorySkill,
  useHarnessInventory,
} from "@/hooks/useHarnessInventory";
import {
  type CatalogKind,
  MOCK_DISCOVER,
  MOCK_SKILL_CONTENT,
  mockTools,
  notAvailableYet,
} from "./harnessCatalogMocks";

const KINDS: { id: CatalogKind; label: string; noun: string }[] = [
  { id: "mcps", label: "MCP servers", noun: "MCP servers" },
  { id: "skills", label: "Skills", noun: "skills" },
  { id: "plugins", label: "Plugins", noun: "plugins" },
];

type OpenItem =
  | { kind: "mcps"; item: InventoryMcpServer }
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
 * MCP servers / Skills / Plugins tabs of a harness, each with a "Yours" list
 * read from the host and a "Discover" catalog. Opening a row replaces the page
 * (header included) with that item's details; Back returns to the same tab.
 */
export function HarnessCatalog({
  header,
  harnessName,
  host,
  family,
}: {
  header: ReactNode;
  harnessName: string;
  host: Host;
  family: BrandHarness;
}) {
  const [tab, setTab] = useState<CatalogKind>("mcps");
  const [source, setSource] = useState<"yours" | "discover">("yours");
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

  if (open?.kind === "mcps") return <McpServerPage server={open.item} onBack={back} />;
  if (open?.kind === "skills") return <SkillPage skill={open.item} onBack={back} />;
  if (open?.kind === "plugins") {
    return <PluginPage plugin={open.item} mcps={open.mcps} onBack={back} />;
  }

  const ownList = (kind: CatalogKind, rows: ReactNode[]) => {
    const noun = KINDS.find((k) => k.id === kind)?.noun;
    if (loading) return <Notice>Loading {noun}…</Notice>;
    if (failed(kind))
      return (
        <Notice>
          Couldn't load {noun} from {host.name}.
        </Notice>
      );
    if (rows.length === 0)
      return (
        <Notice>
          No {noun} found on {host.name}.
        </Notice>
      );
    return <ul className="flex flex-col gap-2">{rows}</ul>;
  };

  return (
    <>
      {header}
      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as CatalogKind)}
        componentId="settings.harnesses.tab"
        className="mt-8 gap-4"
      >
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-1">
          <TabsList variant="line">
            {KINDS.map((k) => (
              <TabsTrigger key={k.id} value={k.id} data-testid={`harness-tab-${k.id}`}>
                {loading ? k.label : `${k.label} · ${own[k.id].length}`}
              </TabsTrigger>
            ))}
          </TabsList>
          <Tabs value={source} onValueChange={(v) => setSource(v as "yours" | "discover")}>
            <TabsList>
              <TabsTrigger value="yours">Yours</TabsTrigger>
              <TabsTrigger value="discover">Discover</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        {source === "discover" ? (
          KINDS.map((k) => (
            <TabsContent key={k.id} value={k.id}>
              <DiscoverList kind={k.id} noun={k.noun} />
            </TabsContent>
          ))
        ) : (
          <>
            <TabsContent value="mcps">
              {ownList(
                "mcps",
                own.mcps.map((server) => (
                  <CatalogRow
                    key={server.id}
                    icon={<LetterAvatar name={server.name} />}
                    name={server.name}
                    detail={server.detail}
                    onOpen={() => setOpen({ kind: "mcps", item: server })}
                  />
                )),
              )}
            </TabsContent>
            <TabsContent value="skills" className="flex flex-col gap-3">
              <AddSkillMenu harnessName={harnessName} />
              {ownList(
                "skills",
                own.skills.map((skill) => (
                  <CatalogRow
                    key={skill.id}
                    icon={<SparkleIcon className="size-4 text-muted-foreground" />}
                    name={skill.name}
                    detail={skill.description}
                    onOpen={() => setOpen({ kind: "skills", item: skill })}
                    trailing={<ItemMenu name={skill.name} />}
                  />
                )),
              )}
            </TabsContent>
            <TabsContent value="plugins">
              {ownList(
                "plugins",
                own.plugins.map((plugin) => {
                  const mcps = pluginMcps(plugin);
                  return (
                    <CatalogRow
                      key={plugin.id}
                      icon={<PlugIcon className="size-4 text-muted-foreground" />}
                      name={plugin.name}
                      detail={`${plural(plugin.skills.length, "skill")} · ${plural(mcps.length, "MCP")}`}
                      onOpen={() => setOpen({ kind: "plugins", item: plugin, mcps })}
                      trailing={<ItemMenu name={plugin.name} />}
                    />
                  );
                }),
              )}
            </TabsContent>
          </>
        )}
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

/** One bordered list row; the main area opens the item when `onOpen` is set. */
function CatalogRow({
  icon,
  name,
  detail,
  onOpen,
  trailing,
}: {
  icon: ReactNode;
  name: string;
  detail?: string;
  onOpen?: () => void;
  trailing?: ReactNode;
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
        "flex items-center gap-2 rounded-xl border border-border pr-2 transition-colors",
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

function ItemMenu({ name }: { name: string }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`More options for ${name}`}>
          <EllipsisVerticalIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={notAvailableYet}>Remove</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function AddSkillMenu({ harnessName }: { harnessName: string }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" className="self-end">
          <PlusIcon />
          Add
          <ChevronDownIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={notAvailableYet}>Upload skill</DropdownMenuItem>
        <DropdownMenuItem onSelect={notAvailableYet}>Create a skill</DropdownMenuItem>
        <DropdownMenuItem onSelect={notAvailableYet}>Create with {harnessName}</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function DiscoverList({ kind, noun }: { kind: CatalogKind; noun: string }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-ui text-muted-foreground">Suggested {noun}</p>
      <ul className="flex flex-col gap-2">
        {MOCK_DISCOVER[kind].map((item) => (
          <CatalogRow
            key={item.name}
            icon={
              kind === "mcps" ? (
                <LetterAvatar name={item.name} />
              ) : kind === "skills" ? (
                <SparkleIcon className="size-4 text-muted-foreground" />
              ) : (
                <PlugIcon className="size-4 text-muted-foreground" />
              )
            }
            name={item.name}
            detail={item.description}
            trailing={
              <Button variant="outline" size="sm" onClick={notAvailableYet}>
                <PlusIcon />
                Add
              </Button>
            }
          />
        ))}
      </ul>
    </div>
  );
}

function McpServerPage({ server, onBack }: { server: InventoryMcpServer; onBack: () => void }) {
  const tools = mockTools(server.name);
  return (
    <>
      <BackButton label="MCP servers" onClick={onBack} />
      <div className="flex items-start justify-between gap-4 border-b border-border pb-6">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border text-lg text-muted-foreground uppercase">
            {server.name[0]}
          </span>
          <div className="flex min-w-0 flex-col">
            <h1 className="truncate text-2xl font-semibold">{server.name}</h1>
            <span className="text-ui text-muted-foreground">
              {server.detail ?? plural(tools.length, "tool")}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={`Reconnect ${server.name}`}
            onClick={notAvailableYet}
          >
            <RefreshCwIcon />
          </Button>
          <Button variant="outline" size="sm" onClick={notAvailableYet}>
            Disconnect
          </Button>
        </div>
      </div>
      <h2 className="mt-6 text-lg font-medium">Tools</h2>
      <ul className="mt-3 flex flex-wrap gap-2">
        {tools.map((tool) => (
          <li key={tool} className="rounded-md bg-muted px-2.5 py-1 font-mono text-xs">
            {tool}
          </li>
        ))}
      </ul>
    </>
  );
}

function SkillPage({ skill, onBack }: { skill: InventorySkill; onBack: () => void }) {
  return (
    <>
      <BackButton label="Skills" onClick={onBack} />
      <div className="flex items-start justify-between gap-4">
        <h1 className="min-w-0 truncate text-2xl font-semibold">{skill.name}</h1>
        <ItemMenu name={skill.name} />
      </div>
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
      <div className="flex items-start justify-between gap-4">
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
        <ItemMenu name={plugin.name} />
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
