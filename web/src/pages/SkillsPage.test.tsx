// Tests for the Skills page (`/skills`) — the harness-neutral cross-harness
// Skill Registry catalog rendered as a master-detail.
//
// The page composes three data seams (catalog list, per-skill detail, trust
// setting), so we mock the API module `@/lib/skillsApi` and let the real
// `useSkills` TanStack Query hooks + the real page run. This exercises the
// projected browser shapes and the include-other-tools gate without a server.

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
  displayPath: "Included with agent",
});
const WORKSPACE = summary({
  id: "workspace:test-generator",
  name: "test-generator",
  description: "Generate unit tests.",
  origin: "workspace",
  displayPath: ".claude/skills/test-generator",
  hasConflict: true,
});
const PERSONAL_NATIVE = summary({
  id: "personal:claude:data-story",
  name: "data-story",
  description: "Shape analysis into a narrative.",
  origin: "personal",
  displayPath: "~/.claude/skills/data-story",
});
const PERSONAL_OTHER = summary({
  id: "personal:codex:migrate",
  name: "migrate",
  description: "Plan and execute a migration.",
  origin: "personal",
  displayPath: "~/.codex/skills/migrate",
});

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
  vi.mocked(skillsApi.getSkillTrust).mockResolvedValue(false);
  vi.mocked(skillsApi.setSkillTrust).mockImplementation(async (v) => v);
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

    expect(await screen.findByTestId("skills-no-session")).toBeInTheDocument();
    expect(screen.getByText("A running session is required")).toBeInTheDocument();
    // No bound session → the session-scoped catalog is never queried.
    expect(skillsApi.getSkillCatalog).not.toHaveBeenCalled();
    // The trust switch is disabled with no session to widen.
    expect(screen.getByTestId("include-other-tools")).toBeDisabled();
  });

  it("queries the catalog scoped to the active session", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    expect(skillsApi.getSkillCatalog).toHaveBeenCalledWith("sess_active", false);
  });

  it("renders a flat, harness-neutral list with NO source/scope section headings", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE],
      includeOtherTools: false,
      hiddenCount: 1,
    } satisfies SkillCatalog);

    renderPage();

    // Every skill appears in one flat list.
    expect(await screen.findByTestId("skill-row-ship")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-test-generator")).toBeInTheDocument();
    expect(screen.getByTestId("skill-row-data-story")).toBeInTheDocument();

    // No source-root / scope section headings anywhere.
    expect(screen.queryByTestId(/^skills-group-/)).toBeNull();
    const list = screen.getByTestId("skills-list");
    expect(within(list).queryByText("Workspace")).toBeNull();
    expect(within(list).queryByText("Personal")).toBeNull();
    expect(within(list).queryByText(".claude/skills")).toBeNull();
    expect(within(list).queryByText("Included with agent")).toBeNull();

    // First skill auto-selected → detail shows its heading.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-skill-id", "bundle:ship");
    expect(within(detailPane).getByRole("heading", { name: "/ship" })).toBeInTheDocument();
  });

  it("offers dynamic source-filter options (distinct roots + All sources)", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    const filter = (await screen.findByTestId("skills-source-filter")) as HTMLSelectElement;
    const options = [...filter.options].map((o) => o.textContent);
    // "All sources" default, then the distinct roots present in the catalog.
    expect(options).toEqual([
      "All sources",
      "Included with agent",
      ".claude/skills",
      "~/.claude/skills",
      "~/.codex/skills",
    ]);
  });

  it("filters the list by source, composing with text search", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-ship");

    // Pick the ~/.codex/skills source → only `migrate` remains.
    fireEvent.change(screen.getByTestId("skills-source-filter"), {
      target: { value: "~/.codex/skills" },
    });
    expect(screen.getByTestId("skill-row-migrate")).toBeInTheDocument();
    expect(screen.queryByTestId("skill-row-ship")).toBeNull();
    expect(screen.queryByTestId("skill-row-data-story")).toBeNull();

    // A search that excludes `migrate` composes with the source filter → empty.
    fireEvent.change(screen.getByTestId("skills-search"), { target: { value: "ship" } });
    expect(screen.queryByTestId("skill-row-migrate")).toBeNull();
    expect(screen.getByText("No skills match your filters.")).toBeInTheDocument();
  });

  it("keeps the selection valid when the selected skill is filtered out", async () => {
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    // `ship` (bundled) auto-selected first.
    const detailPane = await screen.findByTestId("skill-detail");
    expect(detailPane).toHaveAttribute("data-skill-id", "bundle:ship");

    // Filter to a source that excludes `ship` → selection moves to a visible row.
    fireEvent.change(screen.getByTestId("skills-source-filter"), {
      target: { value: "~/.codex/skills" },
    });
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

  it("toggling include-other-tools persists the trust setting and refetches", async () => {
    // Off: 3 skills, one hidden. On: 4 skills, none hidden.
    vi.mocked(skillsApi.getSkillCatalog).mockImplementation(async (_sessionId, include) =>
      include
        ? {
            skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE, PERSONAL_OTHER],
            includeOtherTools: true,
            hiddenCount: 0,
          }
        : {
            skills: [BUILTIN, WORKSPACE, PERSONAL_NATIVE],
            includeOtherTools: false,
            hiddenCount: 1,
          },
    );

    renderPage();
    await screen.findByTestId("skill-row-ship");
    expect(screen.queryByTestId("skill-row-migrate")).toBeNull();

    fireEvent.click(screen.getByTestId("include-other-tools"));

    // The other-tool skill appears and the trust setting is persisted.
    expect(await screen.findByTestId("skill-row-migrate")).toBeInTheDocument();
    // useMutation invokes mutationFn as (variable, context); assert the first arg.
    expect(vi.mocked(skillsApi.setSkillTrust).mock.calls[0][0]).toBe(true);
    expect(skillsApi.getSkillCatalog).toHaveBeenCalledWith("sess_active", true);
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

  it("hides the source filter when the catalog has a single source root", async () => {
    // Two skills, both from the same `.claude/skills` root → nothing to filter.
    vi.mocked(skillsApi.getSkillCatalog).mockResolvedValue({
      skills: [
        WORKSPACE,
        summary({
          id: "workspace:another",
          name: "another",
          origin: "workspace",
          displayPath: ".claude/skills/another",
        }),
      ],
      includeOtherTools: false,
      hiddenCount: 0,
    });

    renderPage();
    await screen.findByTestId("skill-row-another");
    expect(screen.queryByTestId("skills-source-filter")).toBeNull();
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
