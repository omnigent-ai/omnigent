import { useMemo, useState } from "react";
import {
  type BlocksIcon,
  PlusIcon,
  SearchIcon,
  SparkleIcon,
  SparklesIcon,
  TerminalIcon,
} from "lucide-react";
import { Link } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { CustomizeSubSectionId } from "@/shell/settingsNav";
import { SIDEBAR_ROW } from "@/shell/sidebarStyles";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ComposerAgentIcon } from "@/shell/NewChatDialog";

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

/** Whether a harness is set up locally. Mirrors the states used elsewhere. */
type HarnessInstallStatus = "installed" | "available";

interface Harness {
  id: string;
  name: string;
  description: string;
  status: HarnessInstallStatus;
  /** Native harness slug — drives ComposerAgentIcon's colorful/mono icon. */
  harness: string;
}

// Mock catalog — not wired to real readiness/install data yet.
// TODO: Add real readiness/install data from the API in subsequent PRs.
// This feature is WIP behind the `customize` release feature not enabled by default.
const HARNESSES: Harness[] = [
  {
    id: "claude",
    name: "Claude Code",
    description:
      "Anthropic’s coding agent for understanding codebases, editing files, and running development workflows.",
    status: "installed",
    harness: "claude-native",
  },
  {
    id: "codex",
    name: "Codex",
    description:
      "OpenAI’s coding agent for building features, fixing bugs, and working across repositories.",
    status: "installed",
    harness: "codex-native",
  },
  {
    id: "opencode",
    name: "OpenCode",
    description: "An open-source coding agent for the terminal, IDE, and desktop.",
    status: "installed",
    harness: "opencode-native",
  },
  {
    id: "cursor",
    name: "Cursor",
    description:
      "An AI code editor with agent workflows for navigating, editing, and shipping code.",
    status: "installed",
    harness: "cursor-native",
  },
  {
    id: "pi",
    name: "Pi",
    description: "A minimal, extensible coding agent harness built for terminal workflows.",
    status: "installed",
    harness: "pi-native",
  },
  {
    id: "antigravity",
    name: "Antigravity",
    description:
      "Google’s agent-first development platform for planning and executing software tasks.",
    status: "available",
    harness: "antigravity-native",
  },
  {
    id: "kiro",
    name: "Kiro",
    description:
      "An agentic IDE for spec-driven development, hooks, and production-ready software.",
    status: "available",
    harness: "kiro-native",
  },
  {
    id: "qwen",
    name: "Qwen Code",
    description: "An open-source terminal coding agent powered by Qwen models.",
    status: "available",
    harness: "qwen-native",
  },
  {
    id: "goose",
    name: "Goose",
    description: "An open-source local AI agent for coding, automation, and extensible workflows.",
    status: "available",
    harness: "goose-native",
  },
  {
    id: "kimi",
    name: "Kimi",
    description:
      "A terminal coding agent for editing code, running commands, and completing development tasks.",
    status: "available",
    harness: "kimi-native",
  },
  {
    id: "hermes",
    name: "Hermes",
    description: "A self-improving AI agent that learns reusable skills from experience.",
    status: "available",
    harness: "hermes-native",
  },
];

const HarnessesSection = () => {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return HARNESSES;
    return HARNESSES.filter(
      (h) => h.name.toLowerCase().includes(q) || h.description.toLowerCase().includes(q),
    );
  }, [query]);

  return (
    <div className="min-w-0 flex-1 flex flex-col overflow-hidden">
      <div className="@container flex flex-col overflow-hidden mx-auto w-full max-w-[960px] px-10 pt-8">
        <h1 className="pb-6 text-2xl tracking-tight">Harnesses</h1>
        <div className="mb-6 flex h-8 items-center gap-2 rounded-lg border border-border px-2.5">
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
        {filtered.length === 0 && (
          <p className="text-ui text-muted-foreground">No harnesses match “{query}”.</p>
        )}
        <div className="grid grid-cols-1 gap-3 @[520px]:grid-cols-2 flex-1 overflow-y-auto -mx-10 px-10 pb-30">
          {filtered.map((harness) => (
            <HarnessCard key={harness.id} harness={harness} />
          ))}
        </div>
      </div>
    </div>
  );
};

function HarnessCard({ harness }: { harness: Harness }) {
  const installed = harness.status === "installed";
  return (
    <div className="flex flex-col gap-2 rounded-[20px] border border-border bg-card p-4 transition-colors hover:border-foreground/20">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border [&_img]:size-5 [&_svg]:size-5">
            <ComposerAgentIcon agent={{ name: harness.id, harness: harness.harness }} />
          </div>
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-ui font-medium text-foreground">{harness.name}</span>
            {installed && (
              <span className="text-xs text-green-600 dark:text-green-400">Installed</span>
            )}
          </div>
        </div>
        {!installed && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="size-7 shrink-0"
                aria-label={`Install ${harness.name}`}
                data-testid={`harness-action-${harness.id}`}
                componentId="settings.customize.harness.setup"
              >
                <PlusIcon className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Install</TooltipContent>
          </Tooltip>
        )}
      </div>
      <p className="line-clamp-2 text-ui text-muted-foreground">{harness.description}</p>
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
