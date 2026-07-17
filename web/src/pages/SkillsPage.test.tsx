// Tests for the Skills page (`/skills`) — the harness-neutral cross-harness
// Skill Registry catalog rendered as a master-detail.
//
// The page composes catalog-list + per-skill-detail + file seams, so we mock
// the API module `@/lib/skillsApi` and let the real `useSkills` TanStack Query
// hooks + the real page run. The page is a GLOBAL INVENTORY: it always browses
// all sources and never reads/mutates the persisted execution-trust setting.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillsPage } from "./SkillsPage";
import type { SkillCatalog, SkillDetail, SkillSummary } from "@/lib/skillsApi";

vi.mock("@/lib/skillsApi", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/skillsApi")>()),
  getSkillCatalog: vi.fn(),
  getSkillDetail: vi.fn(),
  getSkillFileTree: vi.fn(),
  getSkillFile: vi.fn(),
  // Spied so tests can assert the page NEVER reads/writes execution trust.
  getSkillTrust: vi.fn(),
  setSkillTrust: vi.fn(),
}));

// Keep the real query hooks; override only the session resolver so the tests
// don't need to mock the chat store + conversations list. A test can set it to
// null to exercise the no-session empty state.
const activeSession = vi.fn<() => string | null>(() => "sess_active");
vi.mock("@/hooks/useSkills", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/useSkills")>()),
  useActiveSkillSession: () => activeSession(),
}));

import * as skillsApi from "@/lib/skillsApi";

// clipboard.writeText is used by the copy button; stub it so the jsdom env
// doesn't throw when the test clicks copy.
Object.assign(navigator, {
  clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
});

function summary(overrides: Partial<SkillSummary> = {}): SkillSummary {
  return {
    id: "bundle:ship",
    name: "ship",
    description: "Commit, push, and open a PR.",
    origin: "built_in",
    ownership: "omnigent",
    agentName: null,
    agentId: null,
    invokableInCurrentSession: true,
    requiredAgentName: null,
    displayPath: "Included with agent",
    enabled: true,
    available: true,
    hasConflict: false,
    updatedAt: null,
    ...overrides,
  };
}

function detail(overrides: Partial<SkillDetail> = {}): SkillDetail {
  return {
    ...summary(),
    instructions:
      "---\nname: ship\ndescription: Commit → push → PR.\n---\n\n# ship\n\n## Steps\n\n- Stage the intended files\n- Commit and push",
    overview: "Stages files, commits from the diff, pushes, and opens a PR.",
    advanced: {
      discoveryProvider: "omnigent",
      sourceKind: "agent bundle",
      delivery: "Automatic",
      originPath: "bundle://agent/skills/ship",
      canonicalId: "bundle:ship",
      digest: "77b0c31d",
      conflicts: [],
    },
    ...overrides,
  };
}

const BUILTIN = summary({
  id: "bundle:ship",
  name: "ship",
  origin: "built_in",
  ownership: "omnigent",
  agentName: null,
  displayPath: "Included with agent",
});
const WORKSPACE = summary({
  id: "workspace:test-generator",
  name: "test-generator",
  description: "Generate unit tests.",
  origin: "workspace",
  ownership: "local",
  agentName: null,
  displayPath: ".claude/skills/test-generator",
  hasConflict: true,
});
const PERSONAL_NATIVE = summary({
  id: "personal:claude:data-story",
  name: "data-story",
  description: "Shape analysis into a narrative.",
  origin: "personal",
  ownership: "local",
  agentName: null,
  displayPath: "~/.claude/skills/data-story",
});
const PERSONAL_OTHER = summary({
  id: "personal:codex:migrate",
  name: "migrate",
  description: "Plan and execute a migration.",
  origin: "personal",
  ownership: "local",
  agentName: null,
  displayPath: "~/.codex/skills/migrate",
});
const AGENT_BUNDLED = summary({
  id: "bundle:polly:cross-review",
  name: "cross-review",
  description: "Cross-review with peers.",
  origin: "built_in",
  ownership: "agent",
  agentName: "polly",
  agentId: "ag_polly",
  invokableInCurrentSession: true,
  displayPath: "Included with agent",
});

// N local skills named local-0..local-(N-1), for the per-group cap tests.
function localSkills(n: number): SkillSummary[] {
  return Array.from({ length: n }, (_, i) =>
    summary({
      id: `personal:claude:local-${i}`,
      name: `local-${i}`,
      description: `Local skill ${i}.`,
      origin: "personal",
      ownership: "local",
      agentName: null,
      displayPath: `~/.claude/skills/local-${i}`,
    }),
  );
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SkillsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  activeSession.mockReturnValue("sess_active");
  vi.mocked(skillsApi.getSkillDetail).mockImplementation(async (id) => {
    // Derive origin + a matching display path from the canonical-id prefix so
    // the default detail matches the summary the catalog fixtures use
    // (bundle: / workspace: / personal:).
    const origin: SkillDetail["origin"] = id.startsWith("bundle:")
      ? "built_in"
      : id.startsWith("workspace:")
        ? "workspace"
        : "personal";
    const name = id.split(":").pop() ?? id;
    const displayPath =
      origin === "built_in"
        ? "Included with agent"
        : origin === "workspace"
          ? `.claude/skills/${name}`
          : `~/.claude/skills/${name}`;
    return detail({ id, name, origin, displayPath });
  });
  // Default the Files browser to an empty tree so unrelated tests don't need to
  // stub it; the Files describe block overrides these per-test.
  vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([]);
  vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
    path: "SKILL.md",
    size: 0,
    isText: true,
    tooLarge: false,
    text: "",
  });
});

afterEach(cleanup);

describe("SkillsPage", () => {
  it("shows the no-session empty state and skips the catalog call", async () => {
    activeSession.mockReturnValue(null);
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();

    const empty = await screen.findByTestId("skills-no-session");
    expect(within(empty).getByText("A running session is required")).toBeInTheDocument();
    // Neutral, discovery-only copy — does not imply the listed skills execute
    // automatically in the current session.
    expect(
      within(empty).getByText(/Start or open a session to browse the skill inventory/i),
    ).toBeInTheDocument();
    expect(within(empty).queryByText(/available to it/i)).toBeNull();
    expect(within(empty).queryByText(/personal library/i)).toBeNull();
    // No bound session → the session-scoped catalog is never queried.
    expect(skillsApi.getSkillCatalog).not.toHaveBeenCalled();
  });

  it("uses neutral inventory copy in the header (no auto-availability claim)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    expect(
      screen.getByText("Browse reusable skills discovered across your local agent tools."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/available automatically wherever you work/i)).toBeNull();
  });

  it("has no visibility toggle and never reads/writes execution trust", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    // The "Include skills from other tools" switch + copy are gone.
    expect(screen.queryByTestId("include-other-tools")).toBeNull();
    expect(screen.queryByText(/Include skills from other tools/i)).toBeNull();
    expect(screen.queryByText(/Off by default/i)).toBeNull();
    // The page never touches the persisted execution-trust setting.
    expect(skillsApi.getSkillTrust).not.toHaveBeenCalled();
    expect(skillsApi.setSkillTrust).not.toHaveBeenCalled();
  });

  it("always browses the all-source (global) catalog for the active session", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    // include_other_tools is forced true — the global inventory, not the
    // narrowed current-session view.
    expect(skillsApi.getSkillCatalog).toHaveBeenCalledWith("sess_active", true);
  });

  it("groups by OWNERSHIP (Omnigent/Agent/Local), never by vendor/source path", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, AGENT_BUNDLED, WORKSPACE, PERSONAL_NATIVE],
      includeOtherTools: true,
      hiddenCount: 0,
    } satisfies SkillCatalog);

    renderPage();

    // Every skill appears.
    expect(await screen.findByTestId("skill-row-ship")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-cross-review")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-test-generator")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-data-story")).toBeInTheDocument();

    // Ownership sections render (Omnigent + Agent + Local present here).
    expect(screen.getByTestId("skills-section-omnigent")).toBeInTheDocument();
    expect(screen.getByTestId("skills-section-agent")).toBeInTheDocument();
    expect(screen.getByTestId("skills-section-local")).toBeInTheDocument();

    // No vendor / source-path / origin-scope headings.
    const list = screen.getByTestId("skills-list");
    expect(within(list).queryByText("Workspace")).toBeNull();
    expect(within(list).queryByText("Personal")).toBeNull();
    expect(within(list).queryByText(".claude/skills")).toBeNull();
    expect(within(list).queryByText(/Claude Code|Codex|Cursor/)).toBeNull();

    // First skill auto-selected → detail shows its heading.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-skill-id", "bundle:ship");
    expect(within(detailPane).getByRole("heading", { name: "/ship" })).toBeInTheDocument();
  });

  it("orders sections Omnigent → Agent → Local and shows the agent name", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [WORKSPACE, AGENT_BUNDLED, BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    } satisfies SkillCatalog);

    renderPage();
    await screen.findByTestId("skill-row-ship");

    const sections = screen
      .getAllByTestId(/^skills-section-(omnigent|agent|local)$/)
      .map((el) => el.getAttribute("data-testid"));
    expect(sections).toEqual([
      "skills-section-omnigent",
      "skills-section-agent",
      "skills-section-local",
    ]);
    // The Agent section header is neutral ("Agent"); the agent NAME lives in the
    // nested per-agent subgroup.
    const agentSection = screen.getByTestId("skills-section-agent");
    expect(within(agentSection).getByTestId("skills-section-header-agent")).toHaveTextContent(
      "Agent",
    );
    expect(
      within(screen.getByTestId("skills-agent-subgroup-polly")).getByText("polly"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("skills-section-omnigent")).getByText("Omnigent"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("skills-section-local")).getByText("Local"),
    ).toBeInTheDocument();
  });

  it("hides empty ownership sections", async () => {
    // Only local skills → no Omnigent / Agent sections.
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [WORKSPACE, PERSONAL_NATIVE],
      includeOtherTools: true,
      hiddenCount: 0,
    } satisfies SkillCatalog);

    renderPage();
    await screen.findByTestId("skill-row-test-generator");

    expect(screen.getByTestId("skills-section-local")).toBeInTheDocument();
    expect(screen.queryByTestId("skills-section-omnigent")).toBeNull();
    expect(screen.queryByTestId("skills-section-agent")).toBeNull();
  });

  it("has no source filter control (ownership groups are the only IA)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");
    // The Source / All-sources dropdown is gone; text search remains.
    expect(screen.queryByTestId("skills-source-filter")).toBeNull();
    expect(screen.getByTestId("skills-search")).toBeInTheDocument();
  });

  it("keeps the selection valid when the selected skill is filtered out by search", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    // `ship` (bundled) auto-selected first.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-skill-id", "bundle:ship");

    // Search excludes `ship` → selection moves to the first still-visible row.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "migrate" } });
    await waitFor(() => {
      expect(screen.getByTestId("skill-detail")).toHaveAttribute(
        "data-skill-id",
        "personal:codex:migrate",
      );
    });
  });

  it("does NOT surface harness/vendor names in the primary UI", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [PERSONAL_NATIVE],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-data-story");
    const detailPane = await screen.findByTestId("skill-detail");

    // "Claude" / "Codex" / "Cursor" must not appear until Advanced is opened.
    // The detail fixture's provider is omnigent, so the Source row's secondary
    // provider text reads "Omnigent" — the vendor labels stay hidden.
    expect(within(detailPane).queryByText(/Claude Code|Codex|Cursor/)).toBeNull();
    // The ambiguous scope word "Personal" is NOT shown as provenance; the Source
    // section shows the concrete path instead.
    expect(within(detailPane).queryByText("Personal")).toBeNull();
    expect(within(detailPane).getByTestId("skill-source")).toHaveTextContent(
      "~/.claude/skills/data-story",
    );
  });

  it("does NOT show a 'Ready to use' readiness banner", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    const detailPane = await screen.findByTestId("skill-detail");
    expect(within(detailPane).queryByText(/Ready to use/i)).toBeNull();
    expect(within(detailPane).queryByText(/makes this skill available automatically/i)).toBeNull();
  });

  it("renders no decorative status dot in the status chip or Source chip", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    // The status chip shows text only (no decorative dot span).
    const status = await screen.findByTestId("skill-status");
    expect(status).toHaveTextContent("Enabled");
    expect(status.querySelector("span")).toBeNull();
    // The Source chip's leading accent dot is gone (text + provider only).
    const source = screen.getByTestId("skill-source");
    expect(source.querySelector("span[aria-hidden]")).toBeNull();
  });

  it("selecting a row swaps the detail pane", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    fireEvent.click(await screen.findByTestId("skill-row-test-generator"));

    await waitFor(() => {
      expect(screen.getByTestId("skill-detail")).toHaveAttribute(
        "data-skill-id",
        "workspace:test-generator",
      );
    });
  });

  it("filters the list by search query", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "test-gen" } });

    expect(screen.getByTestId("skill-row-test-generator")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-ship")).toBeNull();
    expect(screen.queryByTestId("skill-row-data-story")).toBeNull();
  });

  it("shows a conflict warning + resolution stack under Advanced only", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [WORKSPACE],
      includeOtherTools: false,
      hiddenCount: 0,
    });
    vi.mocked(skillsApi.getSkillDetail).mockResolvedValue(
      detail({
        id: "workspace:test-generator",
        name: "test-generator",
        origin: "workspace",
        hasConflict: true,
        advanced: {
          discoveryProvider: "omnigent",
          sourceKind: "workspace generic",
          delivery: "Automatic",
          originPath: "./.agents/skills/test-generator",
          canonicalId: "workspace:test-generator",
          digest: "9a02ff41",
          conflicts: [
            { coords: "workspace:test-generator", selected: true },
            { coords: "personal:claude:frontend-toolkit/test-generator", selected: false },
          ],
        },
      }),
    );

    renderPage();
    const advanced = await screen.findByTestId("skill-advanced");
    expect(within(advanced).getByText(/name also defined elsewhere/)).toBeInTheDocument();
    // The shadowed candidate's coords render inside the resolution stack.
    expect(
      within(advanced).getByText("personal:claude:frontend-toolkit/test-generator"),
    ).toBeInTheDocument();
  });

  it("renders availability as read-only status (no enable toggle)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [summary({ enabled: false, available: true })],
      includeOtherTools: false,
      hiddenCount: 0,
    });
    vi.mocked(skillsApi.getSkillDetail).mockResolvedValue(
      detail({ enabled: false, available: true }),
    );

    renderPage();
    const detailPane = await screen.findByTestId("skill-detail");
    expect(within(detailPane).getByTestId("skill-status")).toHaveTextContent("Available");
    // No switch/checkbox for per-skill enable in the detail pane.
    expect(within(detailPane).queryByRole("switch")).toBeNull();
  });

  it("shows a loading state then the list", async () => {
    let resolve!: (c: SkillCatalog) => void;
    vi.mocked(skillsApi.getSkillCatalog).mockReturnValue(
      new Promise<SkillCatalog>((r) => {
        resolve = r;
      }),
    );

    renderPage();
    expect(screen.getByText("Loading skills…")).toBeInTheDocument();

    resolve({ skills: [BUILTIN], includeOtherTools: false, hiddenCount: 0 });
    expect(await screen.findByTestId("skill-row-ship")).toBeInTheDocument();
  });

  it("shows an error state with retry when the catalog fails", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockRejectedValueOnce(new Error("boom"));

    renderPage();
    expect(await screen.findByText("Couldn't load skills.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("SkillsPage — left sidebar file explorer", () => {
  // Two skills so we can prove only the SELECTED skill renders a tree.
  function renderWithTwoSkills() {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE],
      includeOtherTools: false,
      hiddenCount: 0,
    });
    return renderPage();
  }

  it("has no top-level chevron or status dot on skill rows", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([]);
    renderWithTwoSkills();

    const row = await screen.findByTestId("skill-row-ship");
    // The row is a selection control, not a disclosure button.
    expect(row).not.toHaveAttribute("aria-expanded");
    // No decorative SVG (chevron / status dot) inside the row button.
    expect(row.querySelector("svg")).toBeNull();
    // The treeitem wrapper reflects selection, not expansion.
    const treeitem = row.closest('[role="treeitem"]');
    expect(treeitem).toHaveAttribute("aria-selected", "true");
    expect(treeitem).not.toHaveAttribute("aria-expanded");
  });

  it("renders the tree only for the selected skill, fetched lazily", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockImplementation(async (id) =>
      id === "bundle:ship"
        ? [
            { path: "SKILL.md", kind: "file", size: 120 },
            { path: "references", kind: "dir", size: null },
            { path: "references/guide.md", kind: "file", size: 42 },
          ]
        : [{ path: "OTHER.md", kind: "file", size: 10 }],
    );

    renderWithTwoSkills();
    // `ship` auto-selects first → its tree shows; the tree fetch is scoped to it.
    await waitFor(() =>
      expect(skillsApi.getSkillFileTree).toHaveBeenCalledWith("bundle:ship", "sess_active", true),
    );
    expect(await screen.findByTestId("skill-file-SKILL.md")).toBeInTheDocument();
    expect(screen.getByTestId("skill-file-dir-references")).toBeInTheDocument();
    expect(screen.getByTestId("skill-file-references/guide.md")).toBeInTheDocument();

    // The OTHER skill's tree is NOT rendered or fetched (only the selected one).
    expect(screen.queryByTestId("skill-file-OTHER.md")).toBeNull();
    expect(skillsApi.getSkillFileTree).not.toHaveBeenCalledWith(
      "workspace:test-generator",
      "sess_active",
      true,
    );
    // No file content fetched until a file is picked.
    expect(skillsApi.getSkillFile).not.toHaveBeenCalled();
  });

  it("moves the open tree when a different skill is selected", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockImplementation(async (id) =>
      id === "bundle:ship"
        ? [{ path: "SKILL.md", kind: "file", size: 120 }]
        : [{ path: "GEN.md", kind: "file", size: 10 }],
    );

    renderWithTwoSkills();
    expect(await screen.findByTestId("skill-file-SKILL.md")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("skill-row-test-generator"));

    // The new skill's tree renders; the previous skill's is gone.
    expect(await screen.findByTestId("skill-file-GEN.md")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-file-SKILL.md")).toBeNull();
  });

  it("previews a picked file in the RIGHT pane, not a left/right duplicate tree", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "references/guide.md", kind: "file", size: 42 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
      path: "references/guide.md",
      size: 42,
      isText: true,
      tooLarge: false,
      text: "# Guide\n\nHello preview",
    });

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-references/guide.md"));

    await waitFor(() =>
      expect(skillsApi.getSkillFile).toHaveBeenCalledWith(
        "bundle:ship",
        "references/guide.md",
        "sess_active",
        true,
      ),
    );
    // Preview renders in the right detail pane.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-selected-file", "references/guide.md");
    expect(within(detailPane).getByText("Hello preview")).toBeInTheDocument();
    // Exactly one file tree exists (the left one) — no duplicate right-pane tree.
    expect(screen.getAllByTestId("skill-files")).toHaveLength(1);
  });

  it("returns the right pane to the skill overview when the skill row is reselected", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "references/guide.md", kind: "file", size: 42 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
      path: "references/guide.md",
      size: 42,
      isText: true,
      tooLarge: false,
      text: "Hello preview",
    });

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-references/guide.md"));
    const detailPane = await screen.findByTestId("skill-detail");
    await waitFor(() => expect(detailPane).toHaveAttribute("data-selected-file"));

    // Clicking the skill row again clears the file selection → overview returns.
    fireEvent.click(screen.getByTestId("skill-row-ship"));
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail")).not.toHaveAttribute("data-selected-file"),
    );
  });

  it("clears file selection + tree when the selected skill is filtered out", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "references/guide.md", kind: "file", size: 42 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
      path: "references/guide.md",
      size: 42,
      isText: true,
      tooLarge: false,
      text: "Hello preview",
    });

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-references/guide.md"));
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail")).toHaveAttribute("data-selected-file"),
    );

    // Search out `ship` → selection moves to `test-generator`, file cleared.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "test-gen" } });
    await waitFor(() => {
      const d = screen.getByTestId("skill-detail");
      expect(d).toHaveAttribute("data-skill-id", "workspace:test-generator");
      expect(d).not.toHaveAttribute("data-selected-file");
    });
  });

  it("shows a non-preview state for a too-large file", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "big.bin", kind: "file", size: 999999 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
      path: "big.bin",
      size: 999999,
      isText: false,
      tooLarge: true,
      text: null,
    });

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-big.bin"));
    expect(await screen.findByText(/too large to preview/i)).toBeInTheDocument();
  });

  it("shows a non-preview state for a binary file", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "logo.png", kind: "file", size: 2048 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockResolvedValue({
      path: "logo.png",
      size: 2048,
      isText: false,
      tooLarge: false,
      text: null,
    });

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-logo.png"));
    expect(await screen.findByText(/isn't previewable as text/i)).toBeInTheDocument();
  });

  it("renders an empty-tree state under the selected skill", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([]);
    renderWithTwoSkills();
    expect(await screen.findByText("No bundled files.")).toBeInTheDocument();
  });

  it("shows a retry when the file read errors", async () => {
    vi.mocked(skillsApi.getSkillFileTree).mockResolvedValue([
      { path: "references/guide.md", kind: "file", size: 42 },
    ]);
    vi.mocked(skillsApi.getSkillFile).mockRejectedValue(new Error("boom"));

    renderWithTwoSkills();
    fireEvent.click(await screen.findByTestId("skill-file-references/guide.md"));

    expect(await screen.findByText(/Couldn't load references\/guide\.md/)).toBeInTheDocument();
    const detailPane = screen.getByTestId("skill-detail");
    expect(within(detailPane).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("SkillsPage — resizable sidebar", () => {
  it("renders an accessible drag-resize handle between the panes", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });

    renderPage();
    const handle = await screen.findByTestId("skills-resize-handle");
    // Uses the shared separator primitive: vertical separator, labelled,
    // keyboard-focusable.
    expect(handle).toHaveAttribute("role", "separator");
    expect(handle).toHaveAttribute("aria-orientation", "vertical");
    expect(handle).toHaveAttribute("aria-label", "Resize skills list");
    expect(handle).toHaveAttribute("tabindex", "0");
  });
});

describe("SkillsPage — collapsible ownership sections", () => {
  function renderThreeCategories() {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, AGENT_BUNDLED, WORKSPACE, PERSONAL_NATIVE],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    return renderPage();
  }

  it("renders section headers as expanded disclosure buttons with counts", async () => {
    renderThreeCategories();
    const header = await screen.findByTestId("skills-section-header-local");
    expect(header).toHaveAttribute("aria-expanded", "true");
    // Count badge (2 local skills: test-generator + data-story).
    expect(within(header).getByText("2")).toBeInTheDocument();
    // Rows visible by default.
    expect(screen.getByTestId("skill-row-test-generator")).toBeInTheDocument();
  });

  it("keeps individual skill rows chevron-free (only headers have chevrons)", async () => {
    renderThreeCategories();
    const row = await screen.findByTestId("skill-row-test-generator");
    expect(row).not.toHaveAttribute("aria-expanded");
    expect(row.querySelector("svg")).toBeNull();
  });

  it("collapsing a section hides its rows but preserves the selection/detail", async () => {
    renderThreeCategories();
    // `ship` (Omnigent) auto-selected first.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-skill-id", "bundle:ship");

    // Collapse the Local section → its rows hide.
    fireEvent.click(screen.getByTestId("skills-section-header-local"));
    await waitFor(() => expect(screen.queryByTestId("skill-row-test-generator")).toBeNull());
    expect(screen.getByTestId("skills-section-header-local")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // Selection + right-pane detail are untouched.
    expect(screen.getByTestId("skill-detail")).toHaveAttribute("data-skill-id", "bundle:ship");
    // Other sections stay expanded.
    expect(screen.getByTestId("skill-row-cross-review")).toBeInTheDocument();
  });

  it("force-opens matching sections during search, then restores collapse state", async () => {
    renderThreeCategories();
    await screen.findByTestId("skill-row-test-generator");

    // Collapse Local.
    fireEvent.click(screen.getByTestId("skills-section-header-local"));
    await waitFor(() => expect(screen.queryByTestId("skill-row-test-generator")).toBeNull());

    // A search matching a Local skill force-opens the section despite the
    // stored collapse preference.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "test-gen" } });
    expect(await screen.findByTestId("skill-row-test-generator")).toBeInTheDocument();
    expect(screen.getByTestId("skills-section-header-local")).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    // Clearing the search restores the user's collapsed preference.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "" } });
    await waitFor(() =>
      expect(screen.getByTestId("skills-section-header-local")).toHaveAttribute(
        "aria-expanded",
        "false",
      ),
    );
  });
});

describe("SkillsPage — instructions layout + progressive disclosure", () => {
  // jsdom doesn't lay out, so drive the overflow measurement by stubbing
  // scrollHeight on the instructions container.
  function stubInstructionsScrollHeight(px: number) {
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
      configurable: true,
      get() {
        return (this as HTMLElement).getAttribute("data-testid") === "skill-instructions" ? px : 0;
      },
    });
  }
  afterEach(() => {
    // Drop the stub so it doesn't leak into other suites.
    delete (HTMLElement.prototype as { scrollHeight?: unknown }).scrollHeight;
  });

  function renderOneSkill() {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    return renderPage();
  }

  it("orders the detail pane Advanced details BEFORE Instructions", async () => {
    stubInstructionsScrollHeight(50);
    renderOneSkill();
    const detailPane = await screen.findByTestId("skill-detail");
    const html = detailPane.innerHTML;
    expect(html.indexOf("Advanced details")).toBeGreaterThan(-1);
    expect(html.indexOf("Instructions")).toBeGreaterThan(-1);
    // Advanced details appears earlier in the DOM than the Instructions section.
    expect(html.indexOf("Advanced details")).toBeLessThan(html.indexOf("Instructions"));
  });

  it("shows no Show more control when instructions are short", async () => {
    stubInstructionsScrollHeight(120); // < collapsed cap → no overflow
    renderOneSkill();
    await screen.findByTestId("skill-instructions");
    expect(screen.queryByTestId("skill-instructions-toggle")).toBeNull();
    expect(screen.queryByTestId("skill-instructions-scrim")).toBeNull();
  });

  it("shows a fade + Show more when instructions overflow, expanding to Show less", async () => {
    stubInstructionsScrollHeight(2000); // >> collapsed cap → overflow
    renderOneSkill();
    await screen.findByTestId("skill-instructions");

    const toggle = await screen.findByTestId("skill-instructions-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("Show more");
    expect(screen.getByTestId("skill-instructions-scrim")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent("Show less");
    // Expanded → the collapse scrim is gone (content no longer clipped).
    expect(screen.queryByTestId("skill-instructions-scrim")).toBeNull();
    // Expanded container caps at a viewport-relative height with internal scroll.
    expect(screen.getByTestId("skill-instructions")).toHaveStyle({ maxHeight: "70vh" });
  });

  it("resets expansion when switching skills", async () => {
    stubInstructionsScrollHeight(2000);
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();

    fireEvent.click(await screen.findByTestId("skill-instructions-toggle"));
    expect(screen.getByTestId("skill-instructions-toggle")).toHaveTextContent("Show less");

    // Switch to another skill → its instructions start collapsed again.
    fireEvent.click(screen.getByTestId("skill-row-test-generator"));
    await waitFor(() =>
      expect(screen.getByTestId("skill-instructions-toggle")).toHaveTextContent("Show more"),
    );
  });

  it("keeps the Rendered/Source toggle + copy working alongside disclosure", async () => {
    stubInstructionsScrollHeight(2000);
    renderOneSkill();
    await screen.findByTestId("skill-instructions");

    // Source mode still available with the disclosure present.
    fireEvent.click(screen.getByRole("button", { name: "Source" }));
    expect(screen.getByTestId("skill-copy")).toBeInTheDocument();
    expect(screen.getByTestId("skill-instructions-toggle")).toBeInTheDocument();
  });
});

describe("SkillsPage — per-group 6-item cap", () => {
  it("shows no control when a group has 6 or fewer skills", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: localSkills(6),
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-local-0");
    // All six visible, no See all control.
    expect(screen.getByTestId("skill-row-local-5")).toBeInTheDocument();
    expect(screen.queryByTestId("skills-group-more-local")).toBeNull();
  });

  it("caps at 6 with a See all control that expands and collapses", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: localSkills(7),
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-local-0");

    // 7th hidden behind the cap.
    expect(screen.queryByTestId("skill-row-local-6")).toBeNull();
    const more = screen.getByTestId("skills-group-more-local");
    expect(more).toHaveTextContent("See all 7");

    // Expand → 7th visible; label flips to Show fewer.
    fireEvent.click(more);
    expect(await screen.findByTestId("skill-row-local-6")).toBeInTheDocument();
    expect(screen.getByTestId("skills-group-more-local")).toHaveTextContent("Show fewer");

    // Collapse back.
    fireEvent.click(screen.getByTestId("skills-group-more-local"));
    await waitFor(() => expect(screen.queryByTestId("skill-row-local-6")).toBeNull());
  });

  it("caps each ownership group independently", async () => {
    // 7 local + 7 omnigent → each capped on its own, one control per group.
    const omni = Array.from({ length: 7 }, (_, i) =>
      summary({
        id: `bundle:omni-${i}`,
        name: `omni-${i}`,
        origin: "built_in",
        ownership: "omnigent",
        agentName: null,
        displayPath: "Included with agent",
      }),
    );
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [...omni, ...localSkills(7)],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-omni-0");

    // Both groups show their own control; each hides its 7th row.
    expect(screen.getByTestId("skills-group-more-omnigent")).toBeInTheDocument();
    expect(screen.getByTestId("skills-group-more-local")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-omni-6")).toBeNull();
    expect(screen.queryByTestId("skill-row-local-6")).toBeNull();

    // Expanding Local does not expand Omnigent.
    fireEvent.click(screen.getByTestId("skills-group-more-local"));
    expect(await screen.findByTestId("skill-row-local-6")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-omni-6")).toBeNull();
  });

  it("bypasses the cap while a search filter is active", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: localSkills(7),
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-local-0");

    // A search matching all 7 (shared "local" substring) shows every match and
    // no cap control.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "local-" } });
    expect(await screen.findByTestId("skill-row-local-6")).toBeInTheDocument();
    expect(screen.queryByTestId("skills-group-more-local")).toBeNull();
  });

  it("keeps a selected skill visible even when it sorts beyond the cap", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: localSkills(8),
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    // Select the 8th (past the cap) by searching to it, clicking, then clearing.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "local-7" } });
    fireEvent.click(await screen.findByTestId("skill-row-local-7"));
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "" } });

    // Back in the capped view, the selected 7th-index row stays visible (added
    // to the preview set) even though it's beyond the first six.
    expect(await screen.findByTestId("skill-row-local-7")).toBeInTheDocument();
    expect(screen.getByTestId("skill-detail")).toHaveAttribute(
      "data-skill-id",
      "personal:claude:local-7",
    );
  });

  it("hides capped rows when the section is collapsed (collapse takes precedence)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: localSkills(7),
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-local-0");

    fireEvent.click(screen.getByTestId("skills-section-header-local"));
    await waitFor(() => expect(screen.queryByTestId("skill-row-local-0")).toBeNull());
    // The cap control is gone too while collapsed.
    expect(screen.queryByTestId("skills-group-more-local")).toBeNull();
  });
});

describe("SkillsPage — per-agent subgroups + availability", () => {
  const POLLY_SKILL = summary({
    id: "agent:ag_polly:cross-review",
    name: "cross-review",
    description: "Cross-review.",
    ownership: "agent",
    agentName: "polly",
    agentId: "ag_polly",
    invokableInCurrentSession: true,
    displayPath: "Included with agent",
  });
  const DEBBY_SKILL = summary({
    id: "agent:ag_debby:debate",
    name: "debate",
    description: "Debate a plan.",
    ownership: "agent",
    agentName: "debby",
    agentId: "ag_debby",
    invokableInCurrentSession: false,
    requiredAgentName: "debby",
    displayPath: "Included with agent",
  });

  it("nests per-agent subgroups under the Agent section (Polly + Debby)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [POLLY_SKILL, DEBBY_SKILL],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-cross-review");

    // Both agents get their own subgroup — one never hides the other.
    const polly = screen.getByTestId("skills-agent-subgroup-polly");
    const debby = screen.getByTestId("skills-agent-subgroup-debby");
    expect(within(polly).getByText("Available in this session")).toBeInTheDocument();
    expect(within(debby).getByText("Use with debby")).toBeInTheDocument();
    // Same skills live in the right subgroups.
    expect(within(polly).getByTestId("skill-row-cross-review")).toBeInTheDocument();
    expect(within(debby).getByTestId("skill-row-debate")).toBeInTheDocument();
  });

  it("orders the invokable (current) agent subgroup first", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [DEBBY_SKILL, POLLY_SKILL],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    renderPage();
    await screen.findByTestId("skill-row-cross-review");
    const subs = screen
      .getAllByTestId(/^skills-agent-subgroup-/)
      .map((el) => el.getAttribute("data-testid"));
    // Polly (invokable) before Debby (browse-only) regardless of catalog order.
    expect(subs).toEqual(["skills-agent-subgroup-polly", "skills-agent-subgroup-debby"]);
  });

  it("shows 'Bundled with <Agent>' + execution scope in the detail for another agent", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [DEBBY_SKILL],
      includeOtherTools: true,
      hiddenCount: 0,
    });
    vi.mocked(skillsApi.getSkillDetail).mockResolvedValue(
      detail({
        id: "agent:ag_debby:debate",
        name: "debate",
        ownership: "agent",
        agentName: "debby",
        agentId: "ag_debby",
        invokableInCurrentSession: false,
        requiredAgentName: "debby",
        displayPath: "Included with agent",
      }),
    );
    renderPage();
    const detailPane = await screen.findByTestId("skill-detail");
    expect(within(detailPane).getByTestId("skill-source")).toHaveTextContent("Bundled with debby");
    expect(within(detailPane).getByTestId("skill-exec-scope")).toHaveTextContent(
      /Only available with debby/i,
    );
  });
});
