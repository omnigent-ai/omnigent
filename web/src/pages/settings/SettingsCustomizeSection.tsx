import { useMemo, useState } from "react";
import {
  type BlocksIcon,
  ChevronDownIcon,
  SearchIcon,
  SparkleIcon,
  SparklesIcon,
  TerminalIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { CustomizeSubSectionId } from "@/shell/settingsNav";
import { SIDEBAR_ROW } from "@/shell/sidebarStyles";
import { ComposerAgentIcon } from "@/shell/NewChatDialog";
import { HarnessSetupDialog } from "@/shell/HarnessSetupDialog";
import { useHosts, type Host } from "@/hooks/useHosts";
import {
  harnessReadinessOnHost,
  harnessUnavailableReasonOnHost,
  harnessWarningBadgeText,
} from "@/lib/harnessSetup";
import { NATIVE_CODING_AGENTS } from "@/lib/nativeCodingAgents";

const SUB_NAV: { id: CustomizeSubSectionId; label: string; icon: typeof BlocksIcon }[] = [
  { id: "harnesses", label: "Harnesses", icon: TerminalIcon },
  { id: "skills", label: "Skills", icon: SparklesIcon },
];

export const SettingsCustomizeSection = ({ subSection }: { subSection: CustomizeSubSectionId }) => {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <nav className="flex w-64 shrink-0 flex-col gap-0 overflow-y-auto border-r border-border px-3 py-3">
        <h2 className="px-2 py-1 text-sm font-normal text-muted-foreground">Customize</h2>
        {SUB_NAV.map((item) => {
          const Icon = item.icon;
          const selected = subSection === item.id;
          return (
            <Button
              key={item.id}
              asChild
              variant="ghost"
              className={cn(
                SIDEBAR_ROW,
                "w-full justify-start border-0 font-normal",
                selected &&
                  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]",
              )}
            >
              <Link
                to={`/settings/customize/${item.id}`}
                data-testid={`settings-customize-nav-${item.id}`}
                componentId={`settings.customize.nav.${item.id}`}
                aria-current={selected ? "page" : undefined}
              >
                <Icon
                  className={cn(
                    "ui-icon",
                    selected ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
                  )}
                />
                {item.label}
              </Link>
            </Button>
          );
        })}
      </nav>
      {subSection === "harnesses" && <HarnessesSection />}
      {subSection === "skills" && <SkillsSection />}
    </div>
  );
};

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

const HarnessesSection = () => {
  const [query, setQuery] = useState("");
  const { data: hosts } = useHosts({ refetchOnFocus: true });
  const [selectedHostId, setSelectedHostId] = useState<string | null>(null);

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

  // Setup dialog target: reuses the composer's install + auth flow.
  const [setupHarness, setSetupHarness] = useState<HarnessEntry | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return HARNESS_ENTRIES;
    return HARNESS_ENTRIES.filter(
      (h) => h.name.toLowerCase().includes(q) || h.description.toLowerCase().includes(q),
    );
  }, [query]);

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="@container mx-auto flex w-full max-w-[960px] flex-col overflow-hidden px-10 pt-8">
        <h1 className="pb-6 text-2xl tracking-tight">Harnesses</h1>
        <div className="mb-6 flex items-center gap-2">
          <HostSelect hosts={sortedHosts} selected={host} onSelect={setSelectedHostId} />
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
        </div>
        {!host && (
          <p className="text-ui text-muted-foreground" data-testid="harness-no-host">
            Connect an online host to see which harnesses are installed and to set them up.
          </p>
        )}
        {filtered.length === 0 && (
          <p className="text-ui text-muted-foreground">No harnesses match “{query}”.</p>
        )}
        <div
          className={cn(
            "-mx-10 grid flex-1 grid-cols-1 gap-3 overflow-y-auto px-10 pb-30 @[520px]:grid-cols-2",
            // No host to resolve status against — dim the catalog to read as inactive.
            !host && "opacity-50",
          )}
        >
          {filtered.map((entry) => (
            <HarnessCard
              key={entry.harness}
              entry={entry}
              host={host}
              onSetup={() => setSetupHarness(entry)}
            />
          ))}
        </div>
      </div>
      <HarnessSetupDialog
        open={setupHarness !== null}
        onOpenChange={(open) => !open && setSetupHarness(null)}
        agentName={setupHarness?.name}
        harness={setupHarness?.harness ?? null}
        host={host}
      />
    </div>
  );
};

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
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0 gap-1.5"
          data-testid="harness-host-select"
          componentId="settings.customize.harness.host"
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
  );
}

function HarnessCard({
  entry,
  host,
  onSetup,
}: {
  entry: HarnessEntry;
  host: Host | null;
  onSetup: () => void;
}) {
  const readiness = harnessReadinessOnHost(entry.harness, host);
  const ready = readiness.state === "available" && readiness.reason === "ready";
  const needsSetup = host !== null && !ready;
  const reason = harnessUnavailableReasonOnHost(entry.harness, host);

  return (
    <div className="flex flex-col gap-2 rounded-[20px] border border-border bg-card p-4 transition-colors hover:border-foreground/20">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border [&_img]:size-5 [&_svg]:size-5">
            <ComposerAgentIcon agent={{ name: entry.agentName, harness: entry.harness }} />
          </div>
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-ui font-medium text-foreground">{entry.name}</span>
            {ready ? (
              <span className="text-xs text-green-600 dark:text-green-400">Installed</span>
            ) : needsSetup ? (
              <span className="text-xs text-amber-600 dark:text-amber-500">
                {harnessWarningBadgeText(reason)}
              </span>
            ) : null}
          </div>
        </div>
        {needsSetup && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 shrink-0"
            data-testid={`harness-action-${entry.harness}`}
            componentId="settings.customize.harness.setup"
            onClick={onSetup}
          >
            Set up
          </Button>
        )}
      </div>
      <p className="line-clamp-2 text-ui text-muted-foreground">{entry.description}</p>
    </div>
  );
}

interface Skill {
  id: string;
  name: string;
  description: string;
}

// Mock catalog — not wired to real skill data yet.
// TODO: Add real skill data from the API in subsequent PRs.
// This feature is WIP behind the `customize` release feature not enabled by default.
const SKILLS: Skill[] = [
  {
    id: "summarization",
    name: "summarization",
    description:
      "This is placeholder text reserved for a skill description. Omnigent-provided defaults will display the description configured in the system. For custom skills, the user provides the description.",
  },
  {
    id: "code-generation",
    name: "code-generation",
    description: "Generate code from natural-language prompts across languages and frameworks.",
  },
  {
    id: "data-analysis",
    name: "data-analysis",
    description: "Explore datasets, compute summaries, and surface trends from structured data.",
  },
  {
    id: "research",
    name: "research",
    description: "Gather, cross-reference, and synthesize information from multiple sources.",
  },
  {
    id: "writing",
    name: "writing",
    description: "Draft and refine prose, from short copy to long-form documents.",
  },
  {
    id: "translation",
    name: "translation",
    description: "Translate text between languages while preserving tone and meaning.",
  },
];

const SkillsSection = () => {
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(SKILLS[0].id);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return SKILLS;
    return SKILLS.filter(
      (s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q),
    );
  }, [query]);

  const selected = SKILLS.find((s) => s.id === selectedId) ?? null;

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <aside
        className="flex w-56 shrink-0 flex-col overflow-hidden border-r border-border px-3 py-3"
        aria-label="Skills navigation"
      >
        <h2 className="px-2 py-1 text-sm font-normal text-muted-foreground">Skills</h2>
        <div className="mb-2 mt-2 flex h-8 items-center gap-2 rounded-lg border border-border px-2">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search"
            aria-label="Search skills"
            data-testid="skill-search"
            className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
          />
        </div>
        <nav className="flex flex-col gap-px overflow-y-auto">
          {filtered.map((skill) => {
            const isSelected = skill.id === selectedId;
            return (
              <button
                key={skill.id}
                type="button"
                onClick={() => setSelectedId(skill.id)}
                aria-current={isSelected ? "page" : undefined}
                data-testid={`skill-nav-${skill.id}`}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left text-ui transition-colors cursor-pointer",
                  isSelected
                    ? "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)]"
                    : "text-foreground hover:bg-muted",
                )}
              >
                <SparkleIcon
                  className={cn(
                    "size-4 shrink-0",
                    isSelected
                      ? "text-[var(--sidebar-active-foreground)]"
                      : "text-muted-foreground",
                  )}
                  aria-hidden
                />
                <span className="min-w-0 flex-1 truncate">{skill.name}</span>
              </button>
            );
          })}
          {filtered.length === 0 && (
            <p className="px-2 py-1 text-ui text-muted-foreground">No skills match “{query}”.</p>
          )}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {selected ? (
          <>
            <div className="mx-auto w-full max-w-[960px] shrink-0 px-10 pb-6 pt-10">
              <h1 className="text-2xl tracking-tight">{selected.name}</h1>
            </div>
            <div className="min-w-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-[960px] px-10 pb-30">
                <div className="flex flex-col gap-1">
                  <span className="text-ui text-muted-foreground">Description</span>
                  <p className="text-ui text-foreground">{selected.description}</p>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="mx-auto w-full max-w-[960px] px-10 pt-10 text-ui text-muted-foreground">
            Select a skill to see its details.
          </div>
        )}
      </div>
    </div>
  );
};
