/**
 * Skills page (`/skills`) — the harness-neutral cross-harness Skill Registry
 * catalog, rendered as a compact master-detail.
 *
 * Layout (a global inventory master-detail):
 *  - A minimal header: title + one-line neutral inventory subtitle (no trust
 *    toggle — the page always browses every discovered source).
 *  - A drag-resizable left explorer (default ~320px, min ~248, max ~560): a
 *    search box, an optional source-path filter, then skills grouped into
 *    collapsible OWNERSHIP sections (Omnigent → Agent · <name> → Local). Skill
 *    rows are chevron-free selection controls; the selected skill's on-disk
 *    resource tree expands inline beneath its row (folder chevrons live inside
 *    that tree). Source paths appear only in the filter + detail, never as the
 *    list's information architecture.
 *  - A persistent detail pane: overview, a Source row (concise path, provider
 *    secondary), a collapsed Advanced details disclosure, then the SKILL.md
 *    Instructions with a measured, always-scrollable Show more / Show less
 *    disclosure. Selecting a file in the left tree previews it here instead.
 *
 * Data comes from `useSkills` (TanStack Query over `/v1/skills`), always in the
 * all-source browse context. The page never reads/mutates execution trust.
 *
 * The page renders inside the AppShell outlet, so it uses `PageScroll`'s
 * header/inset clearing and fills the available height as a master-detail.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangleIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CodeIcon,
  CopyIcon,
  EyeIcon,
  FileIcon,
  FolderIcon,
  Loader2Icon,
  SearchIcon,
  SparklesIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useActiveSkillSession,
  useSkillCatalog,
  useSkillDetail,
  useSkillFile,
  useSkillFileTree,
} from "@/hooks/useSkills";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import { Link } from "@/lib/routing";
import { copyText } from "@/lib/clipboard";
import {
  OWNERSHIP_ORDER,
  ownershipLabel,
  type SkillDetail,
  type SkillFileNode,
  type SkillOrigin,
  type SkillOwnership,
  type SkillSummary,
} from "@/lib/skillsApi";
import { cn } from "@/lib/utils";

// Per-origin accent token, used only for the small dot/glyph tint. `origin`
// stays INTERNAL — it's never shown as a provenance word and never sections the
// list; it only tints the accent dot. The catalog is a flat, harness-neutral
// list; the concrete source path lives in the detail's Source row and the
// optional source filter, never in the list's information architecture.
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

// ── Skill file tree ────────────────────────────────────────────────────────────

/** A node in the nested tree built from the flat backend node list. */
interface FileTreeNode {
  name: string;
  path: string;
  kind: "file" | "dir";
  size: number | null;
  children: FileTreeNode[];
}

/**
 * Fold the backend's flat, pre-sorted node list into a nested tree. The backend
 * already emits parents before children and dirs-before-files alphabetically,
 * so we can insert in order and preserve it without re-sorting.
 */
function buildFileTree(nodes: SkillFileNode[]): FileTreeNode[] {
  const roots: FileTreeNode[] = [];
  const byPath = new Map<string, FileTreeNode>();
  for (const node of nodes) {
    const treeNode: FileTreeNode = {
      name: node.path.split("/").pop() ?? node.path,
      path: node.path,
      kind: node.kind,
      size: node.size,
      children: [],
    };
    byPath.set(node.path, treeNode);
    const slash = node.path.lastIndexOf("/");
    if (slash === -1) {
      roots.push(treeNode);
    } else {
      const parent = byPath.get(node.path.slice(0, slash));
      // A well-formed list always has the parent already; fall back to root if
      // an intermediate dir is somehow absent so nothing silently disappears.
      if (parent) parent.children.push(treeNode);
      else roots.push(treeNode);
    }
  }
  return roots;
}

/** Human-readable byte size, e.g. `1.2 KB`. */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function fileExtension(path: string): string {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : name.toLowerCase();
}

/** True when a markdown file should render as formatted markdown. */
function isMarkdownPath(path: string): boolean {
  const ext = fileExtension(path);
  return ext === "md" || ext === "markdown";
}

export function SkillsPage() {
  // The Skills page is a GLOBAL INVENTORY: it always browses every discovered
  // local tool/harness source, so all catalog/detail/file requests use the
  // all-source visibility context. This is a BROWSE concern only — it never
  // reads or mutates the persisted `/v1/skills/trust` execution-trust setting,
  // which continues to gate what a session auto-loads/executes. The optional
  // Source filter provides narrowing; a skill the current session can't use is
  // surfaced via availability metadata, never hidden from the catalog.
  const ALL_SOURCE_BROWSE = true;

  // The catalog is scoped to a bound session (bundle/workspace/provider skills
  // resolve on that session's runner). Resolve it before querying.
  const sessionId = useActiveSkillSession();
  const catalogQuery = useSkillCatalog(sessionId, ALL_SOURCE_BROWSE);
  const [query, setQuery] = useState("");
  // Selection model for the left tree:
  //  - selectedId : the skill whose resource tree shows inline beneath its row
  //    AND whose detail the right pane shows. Selection IS expansion — there is
  //    no separate collapse state, and only the selected skill fetches a tree,
  //    so a 100+ catalog never preloads every tree.
  //  - selectedFile : a file picked under the selected skill; when set the right
  //    pane previews it, else it shows the skill overview/instructions.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  // The user's collapsed ownership sections (stable keys: omnigent/agent/local).
  // Sections default expanded; this holds only the ones explicitly collapsed.
  const [collapsedSections, setCollapsedSections] = useState<Set<SkillOwnership>>(new Set());

  // Draggable width for the left explorer column (default 320, kept usable at
  // the low end and bounded at the high end so the detail pane keeps room).
  const { width: listWidth, containerRef, handleProps } = useResizableColumn(320, 248, 560);

  const skills = useMemo(() => catalogQuery.data?.skills ?? [], [catalogQuery.data]);

  // While a text search is active, matching sections are force-expanded so
  // results are never hidden behind a collapsed header (and the per-group cap
  // is bypassed) — the user's stored collapse prefs are untouched and restored
  // when the search clears.
  const filtersActive = query.trim() !== "";

  // The harness-neutral list, filtered by text search only. Ordering is the
  // backend's deterministic catalog order (no client-side re-sort).
  const shown = useMemo(() => skills.filter((s) => matchesQuery(s, query)), [skills, query]);

  // Keep the selection valid: auto-select the first VISIBLE row, and if the
  // current selection is filtered out by search, move to the first still-visible
  // row and drop any file selection so the right pane never previews a file from
  // a now-hidden skill.
  useEffect(() => {
    if (!shown.length) {
      if (selectedId !== null) setSelectedId(null);
      if (selectedFile !== null) setSelectedFile(null);
      return;
    }
    if (selectedId === null || !shown.some((s) => s.id === selectedId)) {
      setSelectedId(shown[0].id);
      setSelectedFile(null);
    }
  }, [shown, selectedId, selectedFile]);

  // Click a skill row: select it (its tree shows inline beneath the row) and
  // drop any file selection so the right pane returns to the skill overview.
  // Selecting a different skill moves the open tree to it — there is no separate
  // collapse; selection is expansion.
  const handleSelectSkill = (id: string) => {
    setSelectedId(id);
    setSelectedFile(null);
  };

  // Click a file under the selected skill: select it for right-pane preview.
  const handleSelectFile = (skillId: string, path: string) => {
    setSelectedId(skillId);
    setSelectedFile(path);
  };

  // Toggle a section's collapsed state. Collapsing hides its rows but never
  // changes the selection / right-pane detail.
  const handleToggleSection = (ownership: SkillOwnership) => {
    setCollapsedSections((prev) => {
      const next = new Set(prev);
      if (next.has(ownership)) next.delete(ownership);
      else next.add(ownership);
      return next;
    });
  };

  return (
    <div
      data-testid="skills-page"
      className="flex min-h-0 w-full flex-1 flex-col"
      style={{ paddingTop: "var(--omnigent-header-height)" }}
    >
      <SkillsHeader />
      {sessionId === null ? (
        <NoSessionEmptyState />
      ) : (
        <div className="flex min-h-0 flex-1" ref={containerRef as React.RefObject<HTMLDivElement>}>
          <SkillList
            width={listWidth}
            handleProps={handleProps}
            skills={shown}
            totalVisible={skills.length}
            query={query}
            onQueryChange={setQuery}
            selectedId={selectedId}
            selectedFile={selectedFile}
            onSelectSkill={handleSelectSkill}
            onSelectFile={handleSelectFile}
            collapsedSections={collapsedSections}
            onToggleSection={handleToggleSection}
            filtersActive={filtersActive}
            sessionId={sessionId}
            includeOtherTools={ALL_SOURCE_BROWSE}
            loading={catalogQuery.isLoading}
            error={catalogQuery.isError}
            onRetry={() => void catalogQuery.refetch()}
          />
          <SkillDetailPane
            skillId={selectedId}
            sessionId={sessionId}
            includeOtherTools={ALL_SOURCE_BROWSE}
            selectedFile={selectedFile}
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
        Skills are discovered on a runner. Start or open a session to browse the skill inventory
        available on that machine.
      </p>
      <Button asChild variant="outline" size="sm" className="mt-1">
        <Link to="/">Start a session</Link>
      </Button>
    </div>
  );
}

// ── Header ────────────────────────────────────────────────────────────────────

function SkillsHeader() {
  // The page is a global inventory — no visibility toggle. It always shows every
  // discovered source; the Source filter (in the list) does optional narrowing.
  return (
    <header className="flex shrink-0 items-center gap-5 border-b border-border px-5 py-3">
      <div className="min-w-0">
        <h1 className="font-heading text-lg font-semibold leading-tight tracking-tight">Skills</h1>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          Browse reusable skills discovered across your local agent tools.
        </p>
      </div>
    </header>
  );
}

// ── Master list ────────────────────────────────────────────────────────────────

function SkillList({
  width,
  handleProps,
  skills,
  totalVisible,
  query,
  onQueryChange,
  selectedId,
  selectedFile,
  onSelectSkill,
  onSelectFile,
  collapsedSections,
  onToggleSection,
  filtersActive,
  sessionId,
  includeOtherTools,
  loading,
  error,
  onRetry,
}: {
  width: number;
  handleProps: React.HTMLAttributes<HTMLDivElement> & { role: "separator" };
  skills: SkillSummary[];
  totalVisible: number;
  query: string;
  onQueryChange: (q: string) => void;
  selectedId: string | null;
  selectedFile: string | null;
  onSelectSkill: (id: string) => void;
  onSelectFile: (skillId: string, path: string) => void;
  collapsedSections: Set<SkillOwnership>;
  onToggleSection: (ownership: SkillOwnership) => void;
  filtersActive: boolean;
  sessionId: string;
  includeOtherTools: boolean;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  return (
    <div
      className="relative flex shrink-0 flex-col border-r border-border"
      style={{ width: `${width}px` }}
    >
      {/* Drag-to-resize handle at the right edge (shared useResizableColumn
          primitive). Sits over the border with a subtle hover/active cue. */}
      <div
        {...handleProps}
        aria-label="Resize skills list"
        tabIndex={0}
        data-testid="skills-resize-handle"
        className="absolute inset-y-0 -right-0.5 z-10 w-1 cursor-col-resize transition-colors hover:bg-primary/30 focus-visible:bg-primary/50 active:bg-primary/50"
      />
      <div className="flex shrink-0 flex-col gap-2 border-b border-border px-3 py-2">
        <div className="flex items-center gap-2">
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
      </div>

      <div
        className="min-h-0 flex-1 overflow-y-auto p-1.5"
        data-testid="skills-list"
        role="tree"
        aria-label="Skills"
      >
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
          // Grouped by harness-neutral OWNERSHIP (Omnigent → Agent → Local).
          // These headings express ownership, never a vendor/source-path detail.
          // Empty sections are hidden; each header is a collapsible disclosure
          // (skill ROWS stay chevron-free). While a text search is active,
          // matching sections are force-open so results are never hidden behind
          // a collapsed header — the stored collapse prefs are untouched.
          OWNERSHIP_ORDER.map((ownership) => {
            const rows = skills.filter((s) => s.ownership === ownership);
            if (rows.length === 0) return null;
            const collapsed = collapsedSections.has(ownership) && !filtersActive;
            return (
              <section key={ownership} className="mb-2" data-testid={`skills-section-${ownership}`}>
                <button
                  type="button"
                  onClick={() => onToggleSection(ownership)}
                  aria-expanded={!collapsed}
                  data-testid={`skills-section-header-${ownership}`}
                  className="flex w-full items-center gap-1.5 px-2 pb-1 pt-2 text-left text-[10.5px] font-bold uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
                >
                  <ChevronRightIcon
                    aria-hidden
                    className={cn(
                      "size-3 shrink-0 transition-transform",
                      !collapsed && "rotate-90",
                    )}
                  />
                  <span className="truncate">{ownershipLabel(ownership)}</span>
                  <span className="ml-auto rounded-full bg-muted px-1.5 text-[10px] font-semibold tabular-nums text-muted-foreground">
                    {rows.length}
                  </span>
                </button>
                {!collapsed &&
                  (ownership === "agent" ? (
                    // The Agent section nests per-agent subgroups so multiple
                    // registered agents (Polly, Debby, …) never blur together;
                    // each subgroup reuses the 6-item cap independently.
                    agentSubgroups(rows).map((sub) => (
                      <AgentSubgroup
                        key={sub.key}
                        agentName={sub.agentName}
                        invokable={sub.invokable}
                        rows={sub.rows}
                        capBypassed={filtersActive}
                        selectedId={selectedId}
                        selectedFile={selectedFile}
                        onSelectSkill={onSelectSkill}
                        onSelectFile={onSelectFile}
                        sessionId={sessionId}
                        includeOtherTools={includeOtherTools}
                      />
                    ))
                  ) : (
                    <SkillGroupRows
                      groupKey={ownership}
                      rows={rows}
                      capBypassed={filtersActive}
                      selectedId={selectedId}
                      selectedFile={selectedFile}
                      onSelectSkill={onSelectSkill}
                      onSelectFile={onSelectFile}
                      sessionId={sessionId}
                      includeOtherTools={includeOtherTools}
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

/** One per-agent subgroup within the Agent ownership section. */
interface AgentSubgroupData {
  key: string;
  agentName: string;
  invokable: boolean;
  rows: SkillSummary[];
}

/**
 * Split the Agent section's rows into per-agent subgroups (keyed by agent id,
 * falling back to name), ordering the CURRENT session's agent first, then the
 * rest alphabetically. Each subgroup carries whether it's invokable here.
 */
function agentSubgroups(rows: SkillSummary[]): AgentSubgroupData[] {
  const byAgent = new Map<string, AgentSubgroupData>();
  for (const row of rows) {
    const key = row.agentId ?? row.agentName ?? "agent";
    let group = byAgent.get(key);
    if (!group) {
      group = {
        key,
        agentName: row.agentName ?? "Agent",
        invokable: row.invokableInCurrentSession,
        rows: [],
      };
      byAgent.set(key, group);
    }
    // A subgroup is invokable iff its skills are usable in this session (the
    // bound agent). All rows for one agent share this flag.
    group.invokable = group.invokable || row.invokableInCurrentSession;
    group.rows.push(row);
  }
  return [...byAgent.values()].sort((a, b) => {
    if (a.invokable !== b.invokable) return a.invokable ? -1 : 1;
    return a.agentName.localeCompare(b.agentName);
  });
}

/**
 * A per-agent subgroup: a small agent-name label with an availability hint
 * ("Available in this session" for the bound agent, "Use with <Agent>" for
 * others), then that agent's capped rows.
 */
function AgentSubgroup({
  agentName,
  invokable,
  rows,
  capBypassed,
  selectedId,
  selectedFile,
  onSelectSkill,
  onSelectFile,
  sessionId,
  includeOtherTools,
}: {
  agentName: string;
  invokable: boolean;
  rows: SkillSummary[];
  capBypassed: boolean;
  selectedId: string | null;
  selectedFile: string | null;
  onSelectSkill: (id: string) => void;
  onSelectFile: (skillId: string, path: string) => void;
  sessionId: string;
  includeOtherTools: boolean;
}) {
  return (
    <div className="mb-1" data-testid={`skills-agent-subgroup-${agentName}`}>
      <div className="flex items-baseline gap-2 px-2 pb-0.5 pt-1">
        <span className="truncate text-[11px] font-semibold text-foreground">{agentName}</span>
        <span className="shrink-0 text-[10px] text-muted-foreground">
          {invokable ? "Available in this session" : `Use with ${agentName}`}
        </span>
      </div>
      <SkillGroupRows
        groupKey={`agent:${agentName}`}
        rows={rows}
        capBypassed={capBypassed}
        selectedId={selectedId}
        selectedFile={selectedFile}
        onSelectSkill={onSelectSkill}
        onSelectFile={onSelectFile}
        sessionId={sessionId}
        includeOtherTools={includeOtherTools}
      />
    </div>
  );
}

// A group shows at most this many skill rows before offering "See all N".
const SKILL_GROUP_CAP = 6;

/**
 * The rows for ONE group, with a per-group 6-item cap + See all / Show fewer.
 * Each group instance owns its own capped state, so capping is independent
 * across groups (and reused verbatim for per-agent subgroups). Rules:
 *   - ≤ CAP rows → no control, all shown.
 *   - An active text search (`capBypassed`) shows every match — no cap.
 *   - The selected skill is always kept visible even if it sorts past the cap,
 *     so collapsing to the preview set never hides the current selection.
 */
function SkillGroupRows({
  groupKey,
  rows,
  capBypassed,
  selectedId,
  selectedFile,
  onSelectSkill,
  onSelectFile,
  sessionId,
  includeOtherTools,
}: {
  groupKey: string;
  rows: SkillSummary[];
  capBypassed: boolean;
  selectedId: string | null;
  selectedFile: string | null;
  onSelectSkill: (id: string) => void;
  onSelectFile: (skillId: string, path: string) => void;
  sessionId: string;
  includeOtherTools: boolean;
}) {
  const [showAll, setShowAll] = useState(false);

  const overCap = rows.length > SKILL_GROUP_CAP;
  // Show everything when filtering, when the user expanded this group, or when
  // it fits. Otherwise show the first CAP — but always include the selected row
  // even if it sorts beyond the cap, so selection is never silently hidden.
  const expanded = capBypassed || showAll || !overCap;
  const visible = useMemo(() => {
    if (expanded) return rows;
    const head = rows.slice(0, SKILL_GROUP_CAP);
    const sel = rows.find((s) => s.id === selectedId);
    if (sel && !head.some((s) => s.id === sel.id)) return [...head, sel];
    return head;
  }, [expanded, rows, selectedId]);

  return (
    <>
      {visible.map((skill) => (
        <SkillRow
          key={skill.id}
          skill={skill}
          selected={selectedId === skill.id}
          selectedFile={selectedId === skill.id ? selectedFile : null}
          onSelectSkill={() => onSelectSkill(skill.id)}
          onSelectFile={(path) => onSelectFile(skill.id, path)}
          sessionId={sessionId}
          includeOtherTools={includeOtherTools}
        />
      ))}
      {/* The cap control is hidden while filtering (every match is already
          shown) and when the group fits under the cap. */}
      {overCap && !capBypassed && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          aria-expanded={showAll}
          data-testid={`skills-group-more-${groupKey}`}
          className="flex w-full items-center gap-1 rounded-md px-3 py-1 text-left text-[11px] font-medium text-muted-foreground transition-colors hover:bg-foreground/5 hover:text-foreground"
        >
          {showAll ? "Show fewer" : `See all ${rows.length}`}
          <ChevronDownIcon
            aria-hidden
            className={cn("size-3 transition-transform", showAll && "rotate-180")}
          />
        </button>
      )}
    </>
  );
}

function SkillRow({
  skill,
  selected,
  selectedFile,
  onSelectSkill,
  onSelectFile,
  sessionId,
  includeOtherTools,
}: {
  skill: SkillSummary;
  selected: boolean;
  selectedFile: string | null;
  onSelectSkill: () => void;
  onSelectFile: (path: string) => void;
  sessionId: string;
  includeOtherTools: boolean;
}) {
  // A skill row is a SELECTION control (not a disclosure): clicking it selects
  // the skill, and the selected skill's resource tree renders inline beneath it.
  // No chevron, no status dot — selection alone drives the visible tree, which
  // fetches lazily only for the selected skill.
  return (
    <div role="treeitem" aria-selected={selected}>
      <button
        type="button"
        onClick={onSelectSkill}
        title={`/${skill.name}`}
        data-testid={`skill-row-${skill.name}`}
        className={cn(
          "flex h-12 w-full items-center rounded-lg px-3 text-left transition-colors",
          "hover:bg-foreground/5",
          selected && "bg-info/10 shadow-[inset_2px_0_var(--color-info)]",
          !skill.enabled && "opacity-60",
        )}
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate font-mono text-[13px] font-semibold text-foreground">
            /{skill.name}
          </span>
          <span className="mt-px block truncate text-[11px] text-muted-foreground">
            {skill.description}
          </span>
        </span>
        {!skill.enabled && (
          <span className="ml-2 shrink-0 text-[9.5px] font-bold uppercase tracking-wide text-muted-foreground">
            available
          </span>
        )}
      </button>
      {selected && (
        <div role="group">
          <SkillFileTree
            skillId={skill.id}
            sessionId={sessionId}
            includeOtherTools={includeOtherTools}
            selectedFile={selectedFile}
            onSelectFile={onSelectFile}
          />
        </div>
      )}
    </div>
  );
}

// ── Detail pane ──────────────────────────────────────────────────────────────

function SkillDetailPane({
  skillId,
  sessionId,
  includeOtherTools,
  selectedFile,
}: {
  skillId: string | null;
  sessionId: string;
  includeOtherTools: boolean;
  selectedFile: string | null;
}) {
  const detailQuery = useSkillDetail(skillId, sessionId, includeOtherTools);

  if (skillId === null) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Select a skill to view details
      </div>
    );
  }

  // A file picked in the left tree takes over the right pane as a preview; the
  // skill's own detail loads underneath (so the header/Source still resolve),
  // but we short-circuit to the preview to keep it responsive.
  if (selectedFile !== null) {
    return (
      <div
        className="flex-1 overflow-y-auto px-6 pb-10 pt-5"
        data-testid="skill-detail"
        data-skill-id={skillId}
        data-selected-file={selectedFile}
      >
        <div className="flex items-center gap-2">
          <FileIcon className="size-4 shrink-0 text-muted-foreground" />
          <h2 className="min-w-0 truncate font-mono text-[15px] font-semibold" title={selectedFile}>
            {selectedFile}
          </h2>
        </div>
        <p className="mt-0.5 text-[11px] text-muted-foreground">Skill resource file</p>
        <div className="mt-4">
          <FilePreview
            skillId={skillId}
            sessionId={sessionId}
            includeOtherTools={includeOtherTools}
            filePath={selectedFile}
          />
        </div>
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

      {/* Source — the path is the provenance source of truth (not a scope word).
          For an agent-bundled skill the headline reads "Bundled with <Agent>". */}
      <Section label="Source">
        <div
          className="inline-flex max-w-full items-center gap-2.5 rounded-lg border border-border bg-muted px-3 py-2"
          data-testid="skill-source"
        >
          <span
            aria-hidden
            className="size-2 shrink-0 rounded-full"
            style={{ background: accent }}
          />
          <span className="truncate font-mono text-[13px] font-semibold text-foreground">
            {skill.ownership === "agent" && skill.agentName
              ? `Bundled with ${skill.agentName}`
              : skill.displayPath}
          </span>
          <span className="shrink-0 border-l border-border pl-2.5 text-[11.5px] text-muted-foreground">
            {providerLabel(skill.advanced.discoveryProvider)}
          </span>
        </div>
        {/* Execution scope — browse visibility is global, but usability is
            per-session. Say so explicitly for agent-bundled skills. */}
        {skill.ownership === "agent" && (
          <p className="mt-1.5 text-[11.5px] text-muted-foreground" data-testid="skill-exec-scope">
            {skill.invokableInCurrentSession
              ? "Available in this session."
              : `Only available with ${skill.requiredAgentName ?? skill.agentName ?? "its agent"}. Browsing here does not load it into the current session.`}
          </p>
        )}
      </Section>

      {/* Advanced details — kept above the (long) instructions so the compact
          metadata stays reachable without scrolling past the SKILL.md body. */}
      <AdvancedDetails skill={skill} />

      {/* Instructions — the tall, progressively-disclosed SKILL.md body. */}
      <Section label="Instructions">
        <InstructionsBlock skill={skill} />
      </Section>
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

// Collapsed instructions get a generous but bounded height; expanded is capped
// at a viewport-relative height with internal scroll so the page never grows
// unbounded on an extreme SKILL.md.
const INSTRUCTIONS_COLLAPSED_MAX_PX = 480;

function InstructionsBlock({ skill }: { skill: SkillDetail }) {
  const [mode, setMode] = useState<"rendered" | "source">("rendered");
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // Whether the collapsed content actually overflows its max-height — measured,
  // so short instructions get no fade and no Show more control.
  const [overflowing, setOverflowing] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Reset expansion when the skill (or render mode) changes — a new skill
  // should always start collapsed.
  useEffect(() => {
    setExpanded(false);
  }, [skill.id]);

  // Measure real overflow against the COLLAPSED cap (compare full content height
  // to the collapsed max), re-running on content/size changes via ResizeObserver.
  const measure = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setOverflowing(el.scrollHeight > INSTRUCTIONS_COLLAPSED_MAX_PX + 1);
  }, []);
  useLayoutEffect(() => {
    measure();
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure, skill.instructions, mode]);

  const handleCopy = async () => {
    await copyText(skill.instructions);
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  // The pane is ALWAYS scrollable when it overflows — collapsed just uses a
  // smaller max-height than expanded, so hidden content is reachable by wheel/
  // touch/scrollbar even before pressing Show more (which merely enlarges the
  // viewport). Short instructions size to content with no cap, fade, or button.
  const bounded = overflowing;
  const collapsedAndOverflowing = bounded && !expanded;
  const showControl = overflowing;

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
        <div
          ref={scrollRef}
          id={`skill-instructions-${skill.id}`}
          data-testid="skill-instructions"
          className={cn("px-4 py-3.5", bounded && "overflow-y-auto")}
          style={
            bounded
              ? { maxHeight: expanded ? "70vh" : `${INSTRUCTIONS_COLLAPSED_MAX_PX}px` }
              : undefined
          }
        >
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
        {/* Fade cue — shown while collapsed-and-overflowing to hint there's more
            below the smaller viewport. It is purely decorative and
            pointer-events-none, so wheel / touch / scrollbar all still scroll
            the content beneath it. Works in light + dark via the muted gradient.
            Sits above the scroll area but below the Show more control (which is
            in normal flow underneath). */}
        {collapsedAndOverflowing && (
          <div
            aria-hidden
            data-testid="skill-instructions-scrim"
            className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-muted via-muted/85 to-transparent"
          />
        )}
        {/* Show more / less — in normal flow below the scroll area (collapsed or
            expanded), so it never obscures instructions. Only shown when the
            content actually overflows the collapsed cap. */}
        {showControl && (
          <div className="flex justify-center border-t border-border/60 bg-muted/40 py-1.5">
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls={`skill-instructions-${skill.id}`}
              data-testid="skill-instructions-toggle"
              className="inline-flex items-center gap-1 rounded-full px-3 py-1 text-[11.5px] font-semibold text-foreground transition-colors hover:bg-foreground/8"
            >
              {expanded ? "Show less" : "Show more"}
              <ChevronDownIcon
                className={cn("size-3.5 transition-transform", expanded && "rotate-180")}
              />
            </button>
          </div>
        )}
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

// ── Inline skill file tree (left sidebar) ───────────────────────────────────────

/**
 * The expanded skill's on-disk resource tree, rendered INLINE in the left
 * master sidebar beneath its skill row. Fetched lazily — only mounted while its
 * skill is expanded, so a 100+ catalog never preloads every tree. Picking a
 * file calls `onSelectFile`; the right pane renders the preview. Directory
 * nodes nest one indent level under the skill row (depth starts at 1).
 */
function SkillFileTree({
  skillId,
  sessionId,
  includeOtherTools,
  selectedFile,
  onSelectFile,
}: {
  skillId: string;
  sessionId: string;
  includeOtherTools: boolean;
  selectedFile: string | null;
  onSelectFile: (path: string) => void;
}) {
  const treeQuery = useSkillFileTree(skillId, sessionId, includeOtherTools);
  const tree = useMemo(() => buildFileTree(treeQuery.data ?? []), [treeQuery.data]);

  if (treeQuery.isLoading) {
    return (
      <div
        className="flex items-center gap-2 py-1.5 pl-8 pr-2 text-[11.5px] text-muted-foreground"
        data-testid="skill-files"
      >
        <Loader2Icon className="size-3.5 animate-spin" />
        Loading files…
      </div>
    );
  }
  if (treeQuery.isError) {
    return (
      <div
        className="flex items-center justify-between gap-2 py-1.5 pl-8 pr-2 text-[11.5px] text-muted-foreground"
        data-testid="skill-files"
      >
        <span>Couldn't load files.</span>
        <Button variant="outline" size="sm" onClick={() => void treeQuery.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  if (!tree.length) {
    return (
      <p className="py-1.5 pl-8 pr-2 text-[11.5px] text-muted-foreground" data-testid="skill-files">
        No bundled files.
      </p>
    );
  }

  return (
    <div className="pb-1" data-testid="skill-files">
      {tree.map((node) => (
        <FileTreeNodeRow
          key={node.path}
          node={node}
          depth={1}
          selectedPath={selectedFile}
          onSelectFile={onSelectFile}
        />
      ))}
    </div>
  );
}

/** One row in the file tree — a directory (collapsible) or a file button. */
function FileTreeNodeRow({
  node,
  depth,
  selectedPath,
  onSelectFile,
}: {
  node: FileTreeNode;
  depth: number;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const indent = { paddingLeft: `${depth * 16 + 8}px` };

  if (node.kind === "dir") {
    return (
      <div role="treeitem" aria-expanded={open}>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          style={indent}
          title={node.path}
          data-testid={`skill-file-dir-${node.path}`}
          className="flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-[12px] text-foreground transition-colors hover:bg-foreground/5"
        >
          <ChevronRightIcon
            className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")}
          />
          <FolderIcon className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate font-mono">{node.name}</span>
        </button>
        {open &&
          node.children.map((child) => (
            <FileTreeNodeRow
              key={child.path}
              node={child}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelectFile={onSelectFile}
            />
          ))}
      </div>
    );
  }

  const selected = selectedPath === node.path;
  return (
    <button
      type="button"
      role="treeitem"
      aria-selected={selected}
      onClick={() => onSelectFile(node.path)}
      style={indent}
      title={node.path}
      data-testid={`skill-file-${node.path}`}
      className={cn(
        "flex w-full items-center gap-1.5 rounded-md py-1 pr-2 text-left text-[12px] transition-colors",
        selected ? "bg-info/12 text-info" : "text-foreground hover:bg-foreground/5",
      )}
    >
      <span className="w-3 shrink-0" aria-hidden />
      <FileIcon className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate font-mono">{node.name}</span>
      {node.size != null && (
        <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
          {formatBytes(node.size)}
        </span>
      )}
    </button>
  );
}

/** Preview one picked file: markdown/text/code, or a non-preview state. */
function FilePreview({
  skillId,
  sessionId,
  includeOtherTools,
  filePath,
}: {
  skillId: string;
  sessionId: string;
  includeOtherTools: boolean;
  filePath: string;
}) {
  const fileQuery = useSkillFile(skillId, sessionId, includeOtherTools, filePath);

  if (fileQuery.isLoading) {
    return (
      <div
        className="flex items-center gap-2 px-3 py-3 text-[11.5px] text-muted-foreground"
        data-testid="skill-file-preview"
      >
        <Loader2Icon className="size-3.5 animate-spin" />
        Loading {filePath}…
      </div>
    );
  }
  if (fileQuery.isError || !fileQuery.data) {
    return (
      <div
        className="flex items-center justify-between gap-2 px-3 py-3 text-[11.5px] text-muted-foreground"
        data-testid="skill-file-preview"
      >
        <span>Couldn't load {filePath}.</span>
        <Button variant="outline" size="sm" onClick={() => void fileQuery.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const file = fileQuery.data;
  // Previewable = the backend decoded it as UTF-8 text within the size cap.
  // Extension only tunes markdown-vs-plain rendering (see isMarkdownPath); any
  // decodable text file is shown, so there's no extension allowlist gate.
  if (file.tooLarge || !file.isText || file.text === null) {
    return (
      <div
        className="px-3 py-3 text-[11.5px] text-muted-foreground"
        data-testid="skill-file-preview"
      >
        <p className="font-mono text-foreground">{filePath}</p>
        <p className="mt-1">
          {file.tooLarge
            ? `This file is ${formatBytes(file.size)} — too large to preview here.`
            : "This file isn't previewable as text."}
        </p>
      </div>
    );
  }

  return (
    <div className="max-h-72 overflow-y-auto px-3 py-3" data-testid="skill-file-preview">
      {isMarkdownPath(filePath) ? (
        <div className="prose prose-sm max-w-none dark:prose-invert prose-headings:font-heading prose-pre:bg-background">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{file.text}</ReactMarkdown>
        </div>
      ) : (
        <pre className="whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-muted-foreground">
          {file.text}
        </pre>
      )}
    </div>
  );
}

function AdvancedDetails({ skill }: { skill: SkillDetail }) {
  const { advanced, hasConflict } = skill;
  const rows: Array<[string, string, boolean]> = [
    ["Discovered in", providerLabel(advanced.discoveryProvider), false],
    ["Source kind", advanced.sourceKind, false],
    ["Delivery", advanced.delivery, false],
    ["Full path", advanced.originPath, true],
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
