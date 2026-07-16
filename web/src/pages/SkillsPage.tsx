/**
 * Skills page (`/skills`) — the harness-neutral cross-harness Skill Registry
 * catalog, rendered as a compact master-detail.
 *
 * Layout (from the final hybrid UX):
 *  - A minimal header: title + one-line subtitle, and a right-aligned
 *    "Include skills from other tools" switch (the trust widening).
 *  - A compact ~292px single-column master list: a tight search box, then
 *    collapsible Built in / Workspace / Personal groups of ~48px rows. Each
 *    row shows an enabled/available dot, the `/name`, and a one-line
 *    description.
 *  - A persistent detail pane: overview, origin explainer, the SKILL.md
 *    instructions (rendered / source toggle + copy), a "ready to use" line,
 *    and a single collapsed Advanced details disclosure holding ALL
 *    harness/vendor provenance (provider, path, source kind, delivery,
 *    canonical id, digest, conflict resolution).
 *
 * Data comes from `useSkills` (TanStack Query over `/v1/skills`). The include
 * switch drives the catalog query key so flipping it refetches for that trust
 * mode. Enable/available is READ-ONLY — the backend invented no per-skill
 * mutation, so the dot + label are status, never a toggle.
 *
 * The page renders inside the AppShell outlet, so it uses `PageScroll`'s
 * header/inset clearing and fills the available height as a master-detail.
 */

import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronRightIcon,
  CodeIcon,
  CopyIcon,
  EyeIcon,
  Loader2Icon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useActiveSkillSession,
  useSkillCatalog,
  useSetSkillTrust,
  useSkillDetail,
  useSkillTrust,
} from "@/hooks/useSkills";
import { Link } from "@/lib/routing";
import { copyText } from "@/lib/clipboard";
import {
  originExplainer,
  originLabel,
  type SkillDetail,
  type SkillOrigin,
  type SkillSummary,
} from "@/lib/skillsApi";
import { cn } from "@/lib/utils";

// Group order + per-origin accent token. Accents map onto the brand palette:
// built-in → primary blue-ish, workspace → brand pink, personal → success green.
const GROUP_ORDER: readonly SkillOrigin[] = ["built_in", "workspace", "personal"];

const ORIGIN_ACCENT: Record<SkillOrigin, string> = {
  built_in: "var(--color-info)",
  workspace: "var(--color-brand-accent)",
  personal: "var(--color-success)",
};

function matchesQuery(skill: SkillSummary, query: string): boolean {
  if (!query) return true;
  const q = query.toLocaleLowerCase();
  return (
    skill.name.toLocaleLowerCase().includes(q) || skill.description.toLocaleLowerCase().includes(q)
  );
}

export function SkillsPage() {
  // The include-other-tools switch. Seeded from the persisted trust setting
  // once it loads, then owned locally so the toggle is instant.
  const trustQuery = useSkillTrust();
  const setTrust = useSetSkillTrust();
  const [includeOtherTools, setIncludeOtherTools] = useState(false);
  const [trustSeeded, setTrustSeeded] = useState(false);
  useEffect(() => {
    if (!trustSeeded && trustQuery.data !== undefined) {
      setIncludeOtherTools(trustQuery.data);
      setTrustSeeded(true);
    }
  }, [trustSeeded, trustQuery.data]);

  // The catalog is scoped to a bound session (bundle/workspace/provider skills
  // resolve on that session's runner). Resolve it before querying.
  const sessionId = useActiveSkillSession();
  const catalogQuery = useSkillCatalog(sessionId, includeOtherTools);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<SkillOrigin>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const skills = useMemo(() => catalogQuery.data?.skills ?? [], [catalogQuery.data]);

  // Auto-select the first visible skill once the catalog loads (or when the
  // current selection drops out of scope, e.g. after toggling the switch off).
  useEffect(() => {
    if (!skills.length) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (selectedId === null || !skills.some((s) => s.id === selectedId)) {
      setSelectedId(skills[0].id);
    }
  }, [skills, selectedId]);

  const shown = useMemo(() => skills.filter((s) => matchesQuery(s, query)), [skills, query]);

  const handleToggleInclude = () => {
    const next = !includeOtherTools;
    setIncludeOtherTools(next);
    setTrust.mutate(next);
  };

  const toggleGroup = (origin: SkillOrigin) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(origin)) next.delete(origin);
      else next.add(origin);
      return next;
    });
  };

  return (
    <div
      data-testid="skills-page"
      className="flex min-h-0 w-full flex-1 flex-col"
      style={{ paddingTop: "var(--omnigent-header-height)" }}
    >
      <SkillsHeader
        includeOtherTools={includeOtherTools}
        onToggleInclude={handleToggleInclude}
        hiddenCount={catalogQuery.data?.hiddenCount ?? 0}
        // The trust switch acts on a session's catalog; disable it with no
        // bound session (there's nothing to widen).
        disabled={sessionId === null}
      />
      {sessionId === null ? (
        <NoSessionEmptyState />
      ) : (
        <div className="flex min-h-0 flex-1">
          <SkillList
            skills={shown}
            totalVisible={skills.length}
            query={query}
            onQueryChange={setQuery}
            collapsed={collapsed}
            onToggleGroup={toggleGroup}
            selectedId={selectedId}
            onSelect={setSelectedId}
            loading={catalogQuery.isLoading}
            error={catalogQuery.isError}
            onRetry={() => void catalogQuery.refetch()}
          />
          <SkillDetailPane
            skillId={selectedId}
            sessionId={sessionId}
            includeOtherTools={includeOtherTools}
          />
        </div>
      )}
    </div>
  );
}

// ── No-session empty state ─────────────────────────────────────────────────────

function NoSessionEmptyState() {
  return (
    <div
      className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center"
      data-testid="skills-no-session"
    >
      <div className="grid size-11 place-items-center rounded-xl bg-muted text-muted-foreground">
        <SparklesIcon className="size-5" />
      </div>
      <h2 className="text-base font-semibold">A running session is required</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Skills are resolved from the bound session's runner — its bundle, this project's workspace
        skills, and your personal library. Start or open a session, then come back to browse the
        Built in, Workspace, and Personal skills available to it.
      </p>
      <Button asChild variant="outline" size="sm" className="mt-1">
        <Link to="/">Start a session</Link>
      </Button>
    </div>
  );
}

// ── Header ────────────────────────────────────────────────────────────────────

function SkillsHeader({
  includeOtherTools,
  onToggleInclude,
  hiddenCount,
  disabled,
}: {
  includeOtherTools: boolean;
  onToggleInclude: () => void;
  hiddenCount: number;
  disabled?: boolean;
}) {
  return (
    <header className="flex shrink-0 items-center gap-5 border-b border-border px-5 py-3">
      <div className="min-w-0">
        <h1 className="font-heading text-lg font-semibold leading-tight tracking-tight">Skills</h1>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          Reusable instructions your agents run — available automatically wherever you work.
        </p>
      </div>
      <div className="flex-1" />
      <label className="flex max-w-xs shrink-0 items-center gap-3">
        <span className="text-right">
          <span className="block text-[12.5px] font-semibold leading-tight">
            Include skills from other tools
          </span>
          <span className="mt-0.5 block text-[11px] leading-tight text-muted-foreground">
            {includeOtherTools && hiddenCount === 0
              ? "Skills from other tools are included."
              : "Off by default. Review unfamiliar ones first."}
          </span>
        </span>
        <Switch
          checked={includeOtherTools}
          onCheckedChange={onToggleInclude}
          disabled={disabled}
          aria-label="Include skills from other tools"
          data-testid="include-other-tools"
        />
      </label>
    </header>
  );
}

// ── Master list ────────────────────────────────────────────────────────────────

function SkillList({
  skills,
  totalVisible,
  query,
  onQueryChange,
  collapsed,
  onToggleGroup,
  selectedId,
  onSelect,
  loading,
  error,
  onRetry,
}: {
  skills: SkillSummary[];
  totalVisible: number;
  query: string;
  onQueryChange: (q: string) => void;
  collapsed: Set<SkillOrigin>;
  onToggleGroup: (origin: SkillOrigin) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="flex w-[292px] shrink-0 flex-col border-r border-border">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <div className="flex flex-1 items-center gap-2 rounded-lg bg-muted px-2.5 py-1.5 focus-within:ring-2 focus-within:ring-ring/40">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search skills…"
            autoComplete="off"
            aria-label="Search skills"
            data-testid="skills-search"
            className="w-full min-w-0 bg-transparent text-[13px] outline-none placeholder:text-muted-foreground"
          />
        </div>
        <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] tabular-nums text-muted-foreground">
          {loading ? "—" : totalVisible}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5" data-testid="skills-list">
        {loading ? (
          <div className="flex items-center justify-center gap-2 px-4 py-10 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            Loading skills…
          </div>
        ) : error ? (
          <div className="px-4 py-10 text-center">
            <p className="text-sm text-muted-foreground">Couldn't load skills.</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
              Try again
            </Button>
          </div>
        ) : skills.length === 0 ? (
          <p className="px-4 py-10 text-center text-[12.5px] text-muted-foreground">
            {query ? `No skills match "${query}".` : "No skills available."}
          </p>
        ) : (
          GROUP_ORDER.map((origin) => {
            const items = skills.filter((s) => s.origin === origin);
            if (!items.length) return null;
            const isCollapsed = collapsed.has(origin);
            return (
              <section key={origin} className="mb-1">
                <button
                  type="button"
                  onClick={() => onToggleGroup(origin)}
                  aria-expanded={!isCollapsed}
                  data-testid={`skills-group-${origin}`}
                  className="mt-2 flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[11px] font-bold uppercase tracking-wide text-muted-foreground transition-colors first:mt-0 hover:text-foreground"
                >
                  <ChevronRightIcon
                    className={cn("size-3 transition-transform", !isCollapsed && "rotate-90")}
                  />
                  <span>{originLabel(origin)}</span>
                  <span className="ml-auto rounded-full bg-muted px-1.5 text-[10.5px] font-semibold text-muted-foreground">
                    {items.length}
                  </span>
                </button>
                {!isCollapsed &&
                  items.map((skill) => (
                    <SkillRow
                      key={skill.id}
                      skill={skill}
                      selected={selectedId === skill.id}
                      onSelect={() => onSelect(skill.id)}
                    />
                  ))}
              </section>
            );
          })
        )}
      </div>
    </div>
  );
}

function SkillRow({
  skill,
  selected,
  onSelect,
}: {
  skill: SkillSummary;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      data-testid={`skill-row-${skill.name}`}
      className={cn(
        "flex h-12 w-full items-center gap-2.5 rounded-lg px-2.5 text-left transition-colors",
        "hover:bg-foreground/5",
        selected && "bg-info/10 shadow-[inset_2px_0_var(--color-info)]",
        !skill.enabled && "opacity-60",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 shrink-0 rounded-full",
          skill.enabled ? "bg-success" : "bg-muted-foreground/40",
        )}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-[13px] font-semibold text-foreground">
          /{skill.name}
        </span>
        <span className="mt-px block truncate text-[11px] text-muted-foreground">
          {skill.description}
        </span>
      </span>
      {!skill.enabled && (
        <span className="shrink-0 text-[9.5px] font-bold uppercase tracking-wide text-muted-foreground">
          available
        </span>
      )}
    </button>
  );
}

// ── Detail pane ──────────────────────────────────────────────────────────────

function SkillDetailPane({
  skillId,
  sessionId,
  includeOtherTools,
}: {
  skillId: string | null;
  sessionId: string;
  includeOtherTools: boolean;
}) {
  const detailQuery = useSkillDetail(skillId, sessionId, includeOtherTools);

  if (skillId === null) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Select a skill to view details
      </div>
    );
  }

  if (detailQuery.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Loading…
      </div>
    );
  }

  if (detailQuery.isError || !detailQuery.data) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
        <p>Couldn't load this skill.</p>
        <Button variant="outline" size="sm" onClick={() => void detailQuery.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  return <SkillDetailBody skill={detailQuery.data} />;
}

function SkillDetailBody({ skill }: { skill: SkillDetail }) {
  const accent = ORIGIN_ACCENT[skill.origin];
  const glyph = skill.name.slice(0, 2).toUpperCase();

  return (
    <div
      className="flex-1 overflow-y-auto px-6 pb-10 pt-5"
      data-testid="skill-detail"
      data-skill-id={skill.id}
    >
      {/* Head */}
      <div className="flex items-start gap-3">
        <div
          aria-hidden
          className="grid size-10 shrink-0 place-items-center rounded-xl font-mono text-lg font-bold"
          style={{ color: accent, background: `color-mix(in srgb, ${accent} 13%, transparent)` }}
        >
          {glyph}
        </div>
        <div className="min-w-0">
          <h2 className="font-mono text-xl font-semibold tracking-tight">/{skill.name}</h2>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            Reusable instruction · one Omnigent identity
          </p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {/* Availability is read-only status (no per-skill mutation exists). */}
          <span
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold",
              skill.enabled ? "bg-success/12 text-success" : "bg-muted text-muted-foreground",
            )}
            data-testid="skill-status"
          >
            <span
              aria-hidden
              className={cn(
                "size-1.5 rounded-full",
                skill.enabled ? "bg-success" : "bg-muted-foreground/50",
              )}
            />
            {skill.enabled ? "Enabled" : "Available"}
          </span>
        </div>
      </div>

      {/* Overview */}
      <Section label="Overview">
        <p className="max-w-2xl text-[13.5px] leading-relaxed text-foreground">
          {skill.overview ?? skill.description}
        </p>
      </Section>

      {/* Origin */}
      <Section label="Origin">
        <div className="inline-flex items-center gap-2.5 rounded-lg border border-border bg-muted px-3 py-2">
          <span className="inline-flex items-center gap-2 text-[13px] font-semibold text-foreground">
            <span aria-hidden className="size-2 rounded-full" style={{ background: accent }} />
            {originLabel(skill.origin)}
          </span>
          <span className="border-l border-border pl-2.5 text-[11.5px] text-muted-foreground">
            {originExplainer(skill.origin)}
          </span>
        </div>
      </Section>

      {/* Instructions */}
      <Section label="Instructions">
        <InstructionsBlock skill={skill} />
      </Section>

      {/* Ready line */}
      <div className="mt-4 flex items-start gap-2 rounded-lg bg-success/10 px-3 py-2.5 text-[11.5px] leading-snug text-success">
        <CheckIcon className="mt-px size-3.5 shrink-0" />
        <span>
          Ready to use. Omnigent makes this skill available automatically when it matches your
          request.
        </span>
      </div>

      {/* Advanced details */}
      <AdvancedDetails skill={skill} />
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mt-5">
      <div className="mb-2 text-[10.5px] font-bold uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      {children}
    </div>
  );
}

function InstructionsBlock({ skill }: { skill: SkillDetail }) {
  const [mode, setMode] = useState<"rendered" | "source">("rendered");
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await copyText(skill.instructions);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  return (
    <TooltipProvider>
      <div className="relative overflow-hidden rounded-lg border border-border bg-muted">
        <div className="absolute right-2 top-2 z-10 flex gap-1 rounded-lg border border-border bg-card/90 p-1 backdrop-blur-sm">
          <IconToggle
            active={mode === "rendered"}
            label="Rendered"
            onClick={() => setMode("rendered")}
          >
            <EyeIcon className="size-3.5" />
          </IconToggle>
          <IconToggle active={mode === "source"} label="Source" onClick={() => setMode("source")}>
            <CodeIcon className="size-3.5" />
          </IconToggle>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={handleCopy}
                aria-label="Copy instructions"
                data-testid="skill-copy"
                className={cn(
                  "grid size-6 place-items-center rounded-md transition-colors hover:bg-foreground/8",
                  copied ? "text-success" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
              </button>
            </TooltipTrigger>
            <TooltipContent side="left">{copied ? "Copied" : "Copy"}</TooltipContent>
          </Tooltip>
        </div>
        <div className="max-h-72 overflow-y-auto px-4 py-3.5" data-testid="skill-instructions">
          {mode === "rendered" ? (
            <div className="prose prose-sm max-w-none dark:prose-invert prose-headings:font-heading prose-pre:bg-background">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{skill.instructions}</ReactMarkdown>
            </div>
          ) : (
            <pre className="whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-muted-foreground">
              {skill.instructions}
            </pre>
          )}
        </div>
      </div>
    </TooltipProvider>
  );
}

function IconToggle({
  active,
  label,
  onClick,
  children,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={onClick}
          aria-label={label}
          aria-pressed={active}
          className={cn(
            "grid size-6 place-items-center rounded-md transition-colors",
            active
              ? "bg-info/12 text-info"
              : "text-muted-foreground hover:bg-foreground/8 hover:text-foreground",
          )}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent side="left">{label}</TooltipContent>
    </Tooltip>
  );
}

function AdvancedDetails({ skill }: { skill: SkillDetail }) {
  const { advanced, hasConflict } = skill;
  const rows: Array<[string, string, boolean]> = [
    ["Discovered in", providerLabel(advanced.discoveryProvider), false],
    ["Source kind", advanced.sourceKind, false],
    ["Delivery", advanced.delivery, false],
    ["Vendor directory", advanced.originPath, true],
    ["Canonical id", advanced.canonicalId, true],
    ["Content digest", advanced.digest, true],
  ];

  return (
    <details className="group mt-5 border-t border-border" data-testid="skill-advanced">
      <summary className="flex cursor-pointer list-none items-center gap-2 py-3 text-[11.5px] font-semibold text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
        Advanced details
        {hasConflict && (
          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-warning">
            <AlertTriangleIcon className="size-3" />
            name also defined elsewhere
          </span>
        )}
        <ChevronRightIcon className="ml-auto size-3 transition-transform group-open:rotate-90" />
      </summary>
      <div className="pb-2">
        <dl className="grid grid-cols-[130px_1fr] gap-x-3.5 gap-y-2 text-xs">
          {rows.map(([term, value, mono]) => (
            <div key={term} className="contents">
              <dt className="text-muted-foreground">{term}</dt>
              <dd className="min-w-0 text-foreground">
                {mono ? (
                  <span className="break-all rounded bg-foreground/6 px-1.5 py-0.5 font-mono text-[11px]">
                    {value}
                  </span>
                ) : (
                  value
                )}
              </dd>
            </div>
          ))}
        </dl>

        {hasConflict && advanced.conflicts.length > 0 && (
          <div className="mt-4">
            <div className="mb-2 text-[10.5px] font-bold uppercase tracking-wide text-muted-foreground">
              Resolution
            </div>
            {advanced.conflicts.map((c) => (
              <div
                key={c.coords}
                className={cn(
                  "mb-1.5 flex items-start gap-2.5 rounded-lg border px-2.5 py-2",
                  c.selected ? "border-success/45 bg-success/8" : "border-border bg-muted",
                )}
              >
                <div className="min-w-0 flex-1">
                  <span className="block text-xs font-semibold">
                    {c.selected ? "In use" : "Shadowed"}
                  </span>
                  <span className="mt-0.5 block break-all font-mono text-[11px] text-muted-foreground">
                    {c.coords}
                  </span>
                </div>
                <span
                  className={cn(
                    "shrink-0 rounded-full px-2 py-0.5 text-[9.5px] font-bold",
                    c.selected ? "bg-success text-white" : "bg-border text-muted-foreground",
                  )}
                >
                  {c.selected ? "in use" : "shadowed"}
                </span>
              </div>
            ))}
            <p className="mt-2.5 text-[11px] leading-relaxed text-muted-foreground">
              Omnigent keeps a single winner per name by scope precedence (built in &gt; workspace
              &gt; personal), with deterministic tie-breaks — never discovery order.
            </p>
          </div>
        )}

        <p className="mt-2.5 text-[11px] leading-relaxed text-muted-foreground">
          Delivery is automatic — Omnigent routes this skill to each environment for you. Skill
          instructions and any bundled <span className="font-mono">scripts/</span> are treated as
          untrusted input.
        </p>
      </div>
    </details>
  );
}

/** Friendly label for a discovery provider (Advanced details only). */
function providerLabel(provider: string): string {
  switch (provider) {
    case "omnigent":
      return "Omnigent";
    case "claude":
      return "Claude Code";
    case "codex":
      return "Codex";
    case "cursor":
      return "Cursor";
    default:
      return provider;
  }
}
