// Capabilities tab content for the right-side rail. A read-only view of
// the agent bound to the active session: its merged skills, MCP servers,
// local/builtin function tools, and declared sub-agent tree.
//
// Context-aware via `conversationId`: the fetch is keyed on it, so
// opening a sub-agent re-fetches and shows THAT agent's capabilities.
// Read-only — no add / edit / remove controls here.

import type { ComponentType, ReactNode, SVGProps } from "react";
import { useState } from "react";
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
import {
  useSessionCapabilities,
  type CapabilityMcpServer,
  type SubAgentCapability,
} from "@/hooks/useSessionCapabilities";
import { cn } from "@/lib/utils";

interface CapabilitiesPanelProps {
  /** The active session whose bound-agent capabilities to render. */
  conversationId: string;
}

export function CapabilitiesPanel({ conversationId }: CapabilitiesPanelProps) {
  const { data, isLoading, error } = useSessionCapabilities(conversationId);

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
      <Section icon={SparklesIcon} title="Skills" count={data.skills.length}>
        {data.skills.length === 0 ? (
          <EmptyState>No skills available.</EmptyState>
        ) : (
          <ul className="flex flex-col">
            {data.skills.map((skill) => (
              <NameDescriptionRow
                key={skill.name}
                name={skill.name}
                description={skill.description}
              />
            ))}
          </ul>
        )}
      </Section>

      <Section icon={PlugIcon} title="MCP servers" count={data.mcp_servers.length}>
        {data.mcp_servers.length === 0 ? (
          <EmptyState>No MCP servers configured.</EmptyState>
        ) : (
          <ul className="flex flex-col">
            {data.mcp_servers.map((server) => (
              <McpServerRow key={server.name} server={server} />
            ))}
          </ul>
        )}
      </Section>

      <Section icon={WrenchIcon} title="Local tools" count={data.local_tools.length}>
        {data.local_tools.length === 0 ? (
          <EmptyState>No local tools available.</EmptyState>
        ) : (
          <ul className="flex flex-col">
            {data.local_tools.map((tool) => (
              <NameDescriptionRow key={tool.name} name={tool.name} description={tool.description} />
            ))}
          </ul>
        )}
      </Section>

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

/** A single name + optional description row, shared by skills and tools. */
function NameDescriptionRow({ name, description }: { name: string; description?: string | null }) {
  return (
    <li className="flex flex-col gap-0.5 px-2.5 py-1.5">
      <span className="truncate text-xs font-medium">{name}</span>
      {description && <span className="text-[11px] text-muted-foreground">{description}</span>}
    </li>
  );
}

function McpServerRow({ server }: { server: CapabilityMcpServer }) {
  // stdio servers carry a spawn command; http servers carry a URL.
  const endpoint = server.transport === "stdio" ? server.command : server.url;
  return (
    <li className="flex flex-col gap-0.5 px-2.5 py-1.5">
      <div className="flex items-center gap-1.5">
        <span className="truncate text-xs font-medium">{server.name}</span>
        <Badge className="border-transparent bg-muted text-[10px] text-muted-foreground">
          {server.transport}
        </Badge>
      </div>
      {server.description && (
        <span className="text-[11px] text-muted-foreground">{server.description}</span>
      )}
      {endpoint && (
        <span className="truncate text-[11px] text-muted-foreground/80" title={endpoint}>
          {endpoint}
        </span>
      )}
      {/* Per-server tool discovery needs a runner round-trip, deferred to a
          later slice — the field is always empty for now, so note it quietly
          rather than rendering an empty list. */}
      <span className="text-[11px] italic text-muted-foreground/70">Inspect tools coming soon</span>
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
