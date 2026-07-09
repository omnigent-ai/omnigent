// Capabilities tab content for the right-side rail. A read-only view of
// the agent bound to the active session: its merged skills, MCP servers
// (with per-server tools), local/builtin function tools, and declared
// sub-agent tree.
//
// Context-aware via `conversationId`: the fetch is keyed on it, so
// opening a sub-agent re-fetches and shows THAT agent's capabilities.
// Read-only — no add / edit / remove controls here.
//
// A single panel-level "Only show usable" toggle (default on) governs the
// whole view: it hides skills / tools the agent cannot actually use and,
// when off, reveals them with explanatory badges. Sub-agents are never
// gated by it.

import type { ComponentType, ReactNode, SVGProps } from "react";
import { useId, useMemo, useState } from "react";
import {
  BotIcon,
  ChevronRightIcon,
  CornerDownRightIcon,
  PlugIcon,
  SparklesIcon,
  WrenchIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Switch } from "@/components/ui/switch";
import {
  useSessionCapabilities,
  type CapabilityMcpServer,
  type CapabilitySkill,
  type CapabilityTool,
  type SubAgentCapability,
} from "@/hooks/useSessionCapabilities";
import { cn } from "@/lib/utils";

/** A skill is usable when it's in the agent's scope and not policy-blocked. */
function isSkillUsable(skill: CapabilitySkill): boolean {
  return skill.in_scope && !skill.blocked;
}

/** A tool is usable when it's not policy-blocked. */
function isToolUsable(tool: CapabilityTool): boolean {
  return !tool.blocked;
}

interface CapabilitiesPanelProps {
  /** The active session whose bound-agent capabilities to render. */
  conversationId: string;
}

export function CapabilitiesPanel({ conversationId }: CapabilitiesPanelProps) {
  const { data, isLoading, error } = useSessionCapabilities(conversationId);

  // Panel-level scope filter (default on): governs skills, local tools, and
  // per-server MCP tools. Sub-agents are never gated by it.
  const [onlyInScope, setOnlyInScope] = useState(true);
  const toggleId = useId();

  if (isLoading && !data) {
    return <CenteredMessage>Loading…</CenteredMessage>;
  }
  if (error && !data) {
    return <CenteredMessage>Failed to load capabilities.</CenteredMessage>;
  }
  if (!data) {
    return <CenteredMessage>No capabilities to show.</CenteredMessage>;
  }

  return (
    <div
      data-testid="capabilities-panel"
      className="flex h-full min-h-0 flex-col overflow-y-auto bg-card"
    >
      <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
        <Switch id={toggleId} size="sm" checked={onlyInScope} onCheckedChange={setOnlyInScope} />
        <label htmlFor={toggleId} className="cursor-pointer text-[11px] text-muted-foreground">
          Only show usable
        </label>
      </div>

      <SkillsSection skills={data.skills} onlyInScope={onlyInScope} />

      <McpSection servers={data.mcp_servers} onlyInScope={onlyInScope} />

      <LocalToolsSection tools={data.local_tools} onlyInScope={onlyInScope} />

      <Section icon={BotIcon} title="Sub-agents" count={data.sub_agents.length}>
        {data.sub_agents.length === 0 ? (
          <EmptyState>No sub-agents declared.</EmptyState>
        ) : (
          <ul className="flex flex-col">
            {data.sub_agents.map((sub, i) => (
              <SubAgentRow key={sub.name ?? `sub-${i}`} node={sub} depth={0} />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function CenteredMessage({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-1 items-center justify-center bg-card px-4 py-8 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  count,
  children,
}: {
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  title: string;
  count: number;
  children: ReactNode;
}) {
  // Default open on first render; collapse state lives locally and is not
  // persisted across reloads (kept intentionally simple).
  const [open, setOpen] = useState(true);
  return (
    <Collapsible asChild open={open} onOpenChange={setOpen}>
      <section className="border-b border-border last:border-b-0">
        <h3>
          <CollapsibleTrigger className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-muted-foreground transition-colors hover:text-foreground">
            <ChevronRightIcon
              aria-hidden="true"
              className={cn(
                "size-3.5 shrink-0 transition-transform",
                open ? "rotate-90" : "rotate-0",
              )}
            />
            <Icon className="size-3.5 shrink-0" />
            <span>{title}</span>
            <Badge className="ml-auto border-transparent bg-muted text-muted-foreground tabular-nums">
              {count}
            </Badge>
          </CollapsibleTrigger>
        </h3>
        <CollapsibleContent>
          <div className="pb-1">{children}</div>
        </CollapsibleContent>
      </section>
    </Collapsible>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return <p className="px-2.5 pb-2 text-xs text-muted-foreground">{children}</p>;
}

// The section count badge always reports the USABLE count (in_scope &&
// !blocked) so the header reflects what the agent can actually run,
// regardless of the toggle — the toggle only widens the list to include
// unavailable skills.
function SkillsSection({
  skills,
  onlyInScope,
}: {
  skills: CapabilitySkill[];
  onlyInScope: boolean;
}) {
  const usable = useMemo(() => skills.filter(isSkillUsable), [skills]);
  const unavailableCount = skills.length - usable.length;
  const visible = onlyInScope ? usable : skills;

  return (
    <Section icon={SparklesIcon} title="Skills" count={usable.length}>
      {skills.length === 0 ? (
        <EmptyState>No skills available.</EmptyState>
      ) : visible.length === 0 ? (
        <EmptyState>
          No skills in scope. Toggle off to see {unavailableCount} unavailable{" "}
          {unavailableCount === 1 ? "skill" : "skills"}.
        </EmptyState>
      ) : (
        <ul className="flex flex-col">
          {visible.map((skill) => (
            <SkillRow key={skill.name} skill={skill} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/** A skill row: name + source pill, plus out-of-scope / blocked status badges. */
function SkillRow({ skill }: { skill: CapabilitySkill }) {
  return (
    <li className="flex flex-col gap-0.5 px-2.5 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="truncate text-xs font-medium">{skill.name}</span>
        <Badge className="border-transparent bg-muted text-[10px] text-muted-foreground">
          {skill.source}
        </Badge>
        {!skill.in_scope && (
          <Badge className="border-transparent bg-muted text-[10px] text-muted-foreground/80">
            out of scope
          </Badge>
        )}
        {skill.blocked && (
          <Badge variant="destructive" className="text-[10px]">
            blocked
          </Badge>
        )}
      </div>
      {skill.description && (
        <span className="text-[11px] text-muted-foreground">{skill.description}</span>
      )}
    </li>
  );
}

// Local tools: the count badge reports the usable (non-blocked) count, and
// the toggle hides blocked tools when on / reveals them with a badge when off.
function LocalToolsSection({
  tools,
  onlyInScope,
}: {
  tools: CapabilityTool[];
  onlyInScope: boolean;
}) {
  const usable = useMemo(() => tools.filter(isToolUsable), [tools]);
  const blockedCount = tools.length - usable.length;
  const visible = onlyInScope ? usable : tools;

  return (
    <Section icon={WrenchIcon} title="Local tools" count={usable.length}>
      {tools.length === 0 ? (
        <EmptyState>No local tools available.</EmptyState>
      ) : visible.length === 0 ? (
        <EmptyState>
          No local tools in scope. Toggle off to see {blockedCount} blocked{" "}
          {blockedCount === 1 ? "tool" : "tools"}.
        </EmptyState>
      ) : (
        <ul className="flex flex-col">
          {visible.map((tool) => (
            <ToolRow key={tool.name} tool={tool} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/** A function tool row: name + optional description, with a blocked badge. */
function ToolRow({ tool }: { tool: CapabilityTool }) {
  return (
    <li className="flex flex-col gap-0.5 px-2.5 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="truncate text-xs font-medium">{tool.name}</span>
        {tool.blocked && (
          <Badge variant="destructive" className="text-[10px]">
            blocked
          </Badge>
        )}
      </div>
      {tool.description && (
        <span className="text-[11px] text-muted-foreground">{tool.description}</span>
      )}
    </li>
  );
}

// MCP servers are always listed regardless of the toggle (so their status is
// always visible); the toggle only filters the tools shown within each server.
// The section count badge reports the number of servers.
function McpSection({
  servers,
  onlyInScope,
}: {
  servers: CapabilityMcpServer[];
  onlyInScope: boolean;
}) {
  return (
    <Section icon={PlugIcon} title="MCP servers" count={servers.length}>
      {servers.length === 0 ? (
        <EmptyState>No MCP servers configured.</EmptyState>
      ) : (
        <ul className="flex flex-col">
          {servers.map((server) => (
            <McpServerRow key={server.name} server={server} onlyInScope={onlyInScope} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/** Maps a server status to a badge tone. */
function statusBadgeClass(status: string): string {
  switch (status) {
    case "connected":
      return "border-transparent bg-emerald-500/15 text-emerald-600 dark:text-emerald-400";
    case "failed":
      return "border-transparent bg-destructive/15 text-destructive";
    default:
      return "border-transparent bg-muted text-muted-foreground/80";
  }
}

// A server row is an expandable disclosure: the header shows name, transport,
// and connection status; expanding lists the server's tools (filtered by the
// panel-level toggle). Read-only.
function McpServerRow({
  server,
  onlyInScope,
}: {
  server: CapabilityMcpServer;
  onlyInScope: boolean;
}) {
  const [open, setOpen] = useState(false);
  // stdio servers carry a spawn command; http servers carry a URL.
  const endpoint = server.transport === "stdio" ? server.command : server.url;

  const usableTools = useMemo(() => server.tools.filter(isToolUsable), [server.tools]);
  const blockedCount = server.tools.length - usableTools.length;
  const visibleTools = onlyInScope ? usableTools : server.tools;

  return (
    <li>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger className="flex w-full cursor-pointer flex-col gap-0.5 px-2.5 py-1.5 text-left hover:bg-muted/50">
          <div className="flex items-center gap-1.5">
            <ChevronRightIcon
              aria-hidden="true"
              className={cn(
                "size-3 shrink-0 text-muted-foreground transition-transform",
                open ? "rotate-90" : "rotate-0",
              )}
            />
            <span className="truncate text-xs font-medium">{server.name}</span>
            <Badge className="border-transparent bg-muted text-[10px] text-muted-foreground">
              {server.transport}
            </Badge>
            <Badge className={cn("text-[10px]", statusBadgeClass(server.status))}>
              {server.status}
            </Badge>
            <Badge className="ml-auto border-transparent bg-muted text-[10px] text-muted-foreground tabular-nums">
              {usableTools.length}
            </Badge>
          </div>
          {server.description && (
            <span className="pl-[18px] text-[11px] text-muted-foreground">
              {server.description}
            </span>
          )}
          {endpoint && (
            <span
              className="truncate pl-[18px] text-[11px] text-muted-foreground/80"
              title={endpoint}
            >
              {endpoint}
            </span>
          )}
          {server.error && (
            <span className="pl-[18px] text-[11px] text-destructive">{server.error}</span>
          )}
        </CollapsibleTrigger>
        <CollapsibleContent>
          {server.tools.length === 0 ? (
            <p className="pb-1.5 pl-[30px] pr-2.5 text-[11px] text-muted-foreground">
              No tools discovered.
            </p>
          ) : visibleTools.length === 0 ? (
            <p className="pb-1.5 pl-[30px] pr-2.5 text-[11px] text-muted-foreground">
              No tools in scope. Toggle off to see {blockedCount} blocked{" "}
              {blockedCount === 1 ? "tool" : "tools"}.
            </p>
          ) : (
            <ul className="flex flex-col pb-1">
              {visibleTools.map((tool) => (
                <li key={tool.name} className="flex flex-col gap-0.5 py-1 pl-[30px] pr-2.5">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="truncate text-[11px] font-medium">{tool.name}</span>
                    {tool.blocked && (
                      <Badge variant="destructive" className="text-[10px]">
                        blocked
                      </Badge>
                    )}
                  </div>
                  {tool.description && (
                    <span className="text-[11px] text-muted-foreground">{tool.description}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CollapsibleContent>
      </Collapsible>
    </li>
  );
}

// Indentation: each nesting level steps the left gutter in one notch so the
// declared sub-agent hierarchy reads as a tree.
const SUBAGENT_BASE_PADDING_PX = 10;
const SUBAGENT_DEPTH_STEP_PX = 14;

function SubAgentRow({ node, depth }: { node: SubAgentCapability; depth: number }) {
  return (
    <>
      <li
        className="flex flex-col gap-0.5 py-1.5 pr-2.5"
        style={{ paddingLeft: SUBAGENT_BASE_PADDING_PX + depth * SUBAGENT_DEPTH_STEP_PX }}
      >
        <div className="flex items-center gap-1">
          {depth > 0 && (
            <CornerDownRightIcon
              aria-hidden="true"
              className="-ml-3 size-3 shrink-0 text-muted-foreground/60"
            />
          )}
          <BotIcon
            className={cn("size-3.5 shrink-0 text-muted-foreground", depth > 0 && "-ml-0.5")}
          />
          <span className="truncate text-xs font-medium">{node.name ?? "(unnamed)"}</span>
        </div>
        {node.description && (
          <span className="pl-[18px] text-[11px] text-muted-foreground">{node.description}</span>
        )}
      </li>
      {node.sub_agents.map((child, i) => (
        <SubAgentRow key={child.name ?? `sub-${depth}-${i}`} node={child} depth={depth + 1} />
      ))}
    </>
  );
}
