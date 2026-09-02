import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useProjectConfig, useProjects } from "@/hooks/useConversations";
import type { ProjectConfig } from "@/lib/projectsApi";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";

// A `?project=` visit prefills the composer from the project's STORED config
// (host / working directory / agent / worktree). A field the config leaves
// unset falls through to the composer's generic defaults (last host, recent
// workspace, last-used agent). These tests pin those seeding rules.
const navigateMock = vi.fn();

const RECENT_KEY = "omnigent:recent-workspaces";
const RECENT_WORKSPACE = "/Users/corey/universe/src/foo";
const REPO = "/Users/corey/projects/alpha";

// Mutable so a test can simulate clicking another project's pencil (the
// screen stays mounted; only the param changes).
let searchParams = new URLSearchParams("project=Alpha");
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigateMock,
  useSearchParams: () => [searchParams, vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({
  setPendingInitialPrompt: vi.fn(),
}));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({ data: [] })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  // Opening the agent picker prefetches details per row; no-op here.
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: vi.fn(),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
// The project list + config are the unit under test's inputs — stub the hooks
// so each case controls them without HTTP-layer plumbing.
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: vi.fn(),
  useProjectConfig: vi.fn(),
  // The landing reads useConversations to decide hasNoSessions (the empty-state
  // import affordance); stub it so it doesn't fire an authenticatedFetch that
  // lands at mock.calls[0] and skews these create-POST call assertions.
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  // Stub so the setup dialog's hook doesn't fire its own /v1/harnesses fetch
  // (which would skew the create-flow call-count assertions here).
  useHarnessSetupSteps: () => ({}),
}));

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "corey-laptop",
    owner: "corey",
    status: "online",
    ...overrides,
  };
}

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_hello",
    name: "hello_world",
    display_name: "Hello World",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

function setProjectConfig(config: ProjectConfig | undefined, isLoading = false): void {
  vi.mocked(useProjectConfig).mockReturnValue({ data: config, isLoading } as ReturnType<
    typeof useProjectConfig
  >);
}

function setProjects(
  data: { id: string | null; name: string }[] | undefined,
  isLoading = false,
): void {
  vi.mocked(useProjects).mockReturnValue({ data, isLoading } as ReturnType<typeof useProjects>);
}

/** Serve a git repo (has an is_main worktree) at REPO; [] elsewhere. */
function setRepoIsGit(): void {
  vi.mocked(useHostWorktrees).mockImplementation((hostId, path) => {
    const known = hostId === "host_1" && path === REPO;
    return {
      data: known
        ? ([{ path: REPO, branch: "main", is_main: true, detached: false }] as HostWorktree[])
        : ([] as HostWorktree[]),
      isError: false,
    } as ReturnType<typeof useHostWorktrees>;
  });
}

function renderLanding(): { rerender: (ui: ReactNode) => void; unmount: () => void } {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  const { rerender, unmount } = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return { rerender, unmount };
}

/** Same as renderLanding, but with the managed-sandbox host option enabled. */
function renderSandboxLanding(): { rerender: (ui: ReactNode) => void } {
  const info: ServerInfo = {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: true,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    features: { harness_install: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  const { rerender } = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return { rerender };
}

/** Render with Smart Routing enabled server-side, so a routing-eligible
 *  native agent can actually turn routing on (the plain renderLanding has no
 *  CapabilitiesProvider, so its server info stays "loading" and routing is
 *  never eligible). Returns rerender so a test can re-drive the same mount. */
function renderRoutingLanding(): { rerender: (ui: ReactNode) => void; unmount: () => void } {
  const info: ServerInfo = {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: true,
    smart_routing_sources: { external: true, oss: false },
    features: { harness_install: false },
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  const { rerender, unmount } = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return { rerender, unmount };
}

/**
 * Open the picker and commit (select + close) an agent by clicking its row.
 * The composed agents these tests use live under the "Custom agents"
 * submenu, so drill in when the row isn't already listed inline.
 */
function selectAgent(agentId: string): void {
  fireEvent.pointerDown(screen.getByTestId("new-chat-landing-agent-select"), { button: 0 });
  if (screen.queryByTestId(`new-chat-landing-agent-${agentId}`) == null) {
    fireEvent.click(screen.getByTestId("new-chat-landing-custom-agents"));
  }
  fireEvent.click(screen.getByTestId(`new-chat-landing-agent-${agentId}`));
}

async function submitAndReadBody(): Promise<Record<string, unknown>> {
  vi.mocked(authenticatedFetch).mockResolvedValueOnce({
    ok: true,
    json: () => Promise.resolve({ id: "conv_new" }),
  } as Response);
  fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
    target: { value: "hello" },
  });
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  await waitFor(() => expect(vi.mocked(authenticatedFetch)).toHaveBeenCalled());
  const [, init] = vi.mocked(authenticatedFetch).mock.calls[0] as [string, RequestInit];
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

beforeEach(() => {
  navigateMock.mockReset();
  vi.mocked(authenticatedFetch).mockReset();
  // The landing draft is module-scoped and survives unmount by design; clear it
  // so a case that never submits can't leak its state into the next test.
  resetLandingDraft();
  searchParams = new URLSearchParams("project=Alpha");
  // The module-scoped landing draft survives unmounts by design; clear it so
  // one test's parked draft can't seed the next one.
  resetLandingDraft();
  localStorage.clear();
  // A recent on the host that the generic seeding would use when the config
  // sets no workspace.
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [RECENT_WORKSPACE] }));
  setHostsAndAgents();
  setRepoIsGit();
  setProjects([
    { id: "proj_alpha", name: "Alpha" },
    { id: "proj_beta", name: "Beta" },
  ]);
  // No stored config by default.
  setProjectConfig({});
});

function setHostsAndAgents(): void {
  vi.mocked(useHosts).mockReturnValue({ data: [host()] } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({
    data: [agent(), agent({ id: "ag_other", name: "other", display_name: "Other" })],
  } as ReturnType<typeof useAvailableAgents>);
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen project prefill", () => {
  it("discards project A's parked draft slots so project B's remount seeds B's defaults", async () => {
    const BETA_REPO = "/Users/corey/projects/beta";
    // Distinct stored defaults per project: Alpha pins ag_other + its repo,
    // Beta pins ag_hello + a different repo.
    vi.mocked(useProjectConfig).mockImplementation((id) => {
      const data =
        id === "proj_beta"
          ? { host_id: "host_1", workspace: BETA_REPO, agent_id: "ag_hello" }
          : { host_id: "host_1", workspace: REPO, agent_id: "ag_other" };
      return { data, isLoading: false } as ReturnType<typeof useProjectConfig>;
    });

    // Visit project Alpha: its defaults seed, and unmounting parks them in
    // the module-scoped landing draft (deliberately NOT reset here).
    searchParams = new URLSearchParams("project=Alpha");
    const { unmount } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    unmount();

    // Remount under project Beta: Alpha's drafted agent/workspace must yield
    // to Beta's stored defaults (the prefill writes are fill-empty-only, so a
    // restored draft would otherwise win every slot).
    searchParams = new URLSearchParams("project=Beta");
    renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("beta"),
    );
    const body = await submitAndReadBody();
    // Beta's seeded slots are still config-values, so the create omits them
    // for server default-fill under Beta's project_id. Had Alpha's drafted
    // agent survived, it would differ from Beta's config and be sent
    // explicitly — the omission is the assertion.
    expect(body.project_id).toBe("proj_beta");
    expect("agent_id" in body).toBe(false);
    expect("workspace" in body).toBe(false);
  });

  it("keeps the draft's picked agent on a same-project remount", async () => {
    // Same project back and forth: the draft is the user's in-progress
    // composition for THIS project, so an explicit agent pick must survive
    // the unmount/remount — only a project switch discards it.
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_hello" });
    const { unmount } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    selectAgent("ag_other");
    unmount();

    renderLanding();
    const body = await submitAndReadBody();
    // The explicit pick differs from the config agent, so it rides
    // explicitly; the untouched config workspace is omitted (default-fill).
    expect(body.agent_id).toBe("ag_other");
    expect("workspace" in body).toBe(false);
    expect(body.project_id).toBe("proj_alpha");
  });

  it("carries a pinned session-scoped config agent into the create body over last-agent-id", async () => {
    // The configured agent is only resolvable through discovery's pinning
    // (a session-derived row, absent from the plain catalog); a stored
    // last-agent-id must not displace it.
    localStorage.setItem("omnigent:last-agent-id", "ag_hello");
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        agent(),
        agent({
          id: "ag_pinned",
          name: "deploy-bot",
          display_name: "Deploy-bot",
          sessionId: "conv_anchor",
        }),
      ],
    } as ReturnType<typeof useAvailableAgents>);
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_pinned" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    // The configured agent held (untouched → omitted for default-fill). Had
    // last-agent-id displaced it, the differing agent would be sent explicitly.
    expect(body.project_id).toBe("proj_alpha");
    expect("agent_id" in body).toBe(false);
    expect("workspace" in body).toBe(false);
    // The composer must thread the configured agent into discovery's pins —
    // that's what makes the session-scoped row above resolvable at all.
    expect(
      vi
        .mocked(useAvailableAgents)
        .mock.calls.some(([opts]) => opts?.pinnedAgentIds?.includes("ag_pinned") ?? false),
    ).toBe(true);
  });

  it("surfaces an explicit unavailable state instead of substituting an agent the config pinned", async () => {
    // The configured agent resolves nowhere (catalog, scan, and pinned lookup
    // all missed it). The composer must say so and block submit — not fall
    // back to last-agent-id or the picker's first row.
    localStorage.setItem("omnigent:last-agent-id", "ag_hello");
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_gone" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Agent unavailable",
      ),
    );
    fireEvent.change(screen.getByTestId("new-chat-landing-input"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    expect(vi.mocked(authenticatedFetch)).not.toHaveBeenCalled();

    // Recovery: an explicit pick clears the state and submit goes through
    // with the agent the user actually chose.
    selectAgent("ag_other");
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain("Other"),
    );
    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_other");
  });

  it("seeds host / workspace / agent from the stored config", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    // Untouched config-seeded slots are omitted for server default-fill.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
    expect("agent_id" in body).toBe(false);
    // No opt-in worktree → no git block.
    expect(body.git).toBeUndefined();
  });

  it("creates a fresh worktree when the config opts in", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: true });
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect("workspace" in body).toBe(false);
    // The branch name is generated client-side, so `git` is always explicit.
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("does NOT create a worktree when the config omits use_worktree", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO });
    renderLanding();

    const body = await submitAndReadBody();
    expect("workspace" in body).toBe(false);
    expect(body.git).toBeUndefined();
  });

  it("falls back to the generic defaults when the project has no config", async () => {
    setProjectConfig({});
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.agent_id).toBe("ag_hello");
    expect(body.git).toBeUndefined();
  });

  it("seeds only the host from config, leaving the workspace to the generic default", async () => {
    setProjectConfig({ host_id: "host_1" });
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
  });

  it("waits for the projects list before settling, so a config agent isn't lost to a race", async () => {
    // The projects list resolves name → id; until it loads the id is falsely
    // null. The prefill must WAIT rather than settle from the generic default,
    // or the stored default agent would never apply.
    setProjects(undefined, true); // still loading
    setProjectConfig({ host_id: "host_1", agent_id: "ag_other" });
    const { rerender } = renderLanding();

    // Projects finish loading → config resolves and the agent seeds.
    setProjects([{ id: "proj_alpha", name: "Alpha" }]);
    rerender(<NewChatLandingScreen />);

    const body = await submitAndReadBody();
    // The config agent seeded (and stayed) → omitted for default-fill. A
    // premature settle would have picked the generic default, which differs
    // from the config and would ride explicitly.
    expect(body.project_id).toBe("proj_alpha");
    expect("agent_id" in body).toBe(false);
  });

  it("reseeds from the new project when another pencil is clicked while mounted", async () => {
    const BETA_REPO = "/Users/corey/projects/beta";
    vi.mocked(useProjectConfig).mockImplementation((id) => {
      const data =
        id === "proj_beta"
          ? { host_id: "host_1", workspace: BETA_REPO, agent_id: "ag_other" }
          : { host_id: "host_1", workspace: REPO };
      return { data, isLoading: false } as ReturnType<typeof useProjectConfig>;
    });
    const { rerender } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    searchParams = new URLSearchParams("project=Beta");
    rerender(<NewChatLandingScreen />);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("beta"),
    );
    const body = await submitAndReadBody();
    // Reseeded to Beta's config (the chip check above proves the UI): both
    // slots stayed config-values, omitted under Beta's project_id.
    expect(body.project_id).toBe("proj_beta");
    expect("workspace" in body).toBe(false);
    expect("agent_id" in body).toBe(false);
  });

  it("clears a drafted sandbox repository on an in-place project switch (screen stays mounted)", async () => {
    // Mounted mirror of the unmount/remount sandbox-leak case: clicking
    // another project's pencil only changes `?project=` — the screen never
    // unmounts, so the draft-restore strip can't help. The reset effect must
    // clear the staged repo/branch or project Beta's sandbox silently clones
    // project Alpha's repository.
    const { rerender } = renderSandboxLanding();
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-host-chip"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-sandbox-option"));
    fireEvent.click(screen.getByTestId("new-chat-landing-repo-chip"));
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-input"), {
      target: { value: "https://github.com/org/alpha-repo" },
    });
    fireEvent.change(screen.getByTestId("new-chat-landing-repo-branch-input"), {
      target: { value: "alpha-main" },
    });
    expect(screen.getByTestId("new-chat-landing-repo-chip").textContent).toContain(
      "alpha-repo#alpha-main",
    );

    // Click project Beta's pencil: the param changes in place.
    searchParams = new URLSearchParams("project=Beta");
    rerender(<NewChatLandingScreen />);

    // The sticky host pick re-selects the sandbox, but Alpha's staged repo
    // inputs are gone.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-repo-chip").textContent).toContain("Repository"),
    );
    const body = await submitAndReadBody();
    expect(body.host_type).toBe("managed");
    // Blank repo inputs pin workspace to explicit null under Beta's
    // project_id (a managed create rejects a default-filled path) — not
    // Alpha's repo#branch.
    expect(body.workspace).toBeNull();
  });

  it("reseeds the SAME project after its stored defaults change (edited then re-opened)", async () => {
    const EDITED_REPO = "/Users/corey/projects/alpha-edited";
    // First open reads the original config.
    setProjectConfig({ host_id: "host_1", workspace: REPO });
    const { rerender } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    // User edits the project's defaults; the save seeds the fresh config into
    // the cache, so a re-open of the SAME project (`?project=Alpha` unchanged)
    // must pick up the new workspace rather than latch onto the settled seed.
    setProjectConfig({ host_id: "host_1", workspace: EDITED_REPO });
    rerender(<NewChatLandingScreen />);

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain(
        "alpha-edited",
      ),
    );
    const body = await submitAndReadBody();
    // The chip check above proves the edited workspace reseeded; being a
    // config-value again, the create omits it for default-fill.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
  });

  it("keeps the configured workspace when the config host is offline (host falls back)", async () => {
    vi.mocked(useHosts).mockReturnValue({
      data: [host(), host({ host_id: "host_off", name: "sleepy", status: "offline" })],
    } as ReturnType<typeof useHosts>);
    setProjectConfig({ host_id: "host_off", workspace: "/somewhere" });
    renderLanding();

    const body = await submitAndReadBody();
    // The offline host falls back to the generic default, but the project's
    // workspace hint must not be displaced by the host's recent path (which
    // can belong to another project).
    expect(body.host_id).toBe("host_1");
    // The workspace hint held (still the config value) → omitted; the server
    // default-fills it. A displaced hint would ride as an explicit recent path.
    expect("workspace" in body).toBe(false);
  });

  // A repo with a main work tree plus one linked worktree. `git worktree list`
  // returns both for any path inside the repo, so the probe (keyed on the
  // recent-workspace path) and the post-redirect main query both resolve here.
  const MAIN_REPO = "/Users/corey/projects/gamma";
  const LINKED_WORKTREE = "/Users/corey/projects/gamma-worktrees/feature-x";
  const WORKTREE_LIST: HostWorktree[] = [
    { path: MAIN_REPO, branch: "main", is_main: true, detached: false },
    { path: LINKED_WORKTREE, branch: "feature/x", is_main: false, detached: false },
  ];

  function setWorktreeRepo(): void {
    vi.mocked(useHostWorktrees).mockImplementation((hostId, path) => {
      const inRepo = hostId === "host_1" && (path === MAIN_REPO || path === LINKED_WORKTREE);
      return {
        data: inRepo ? WORKTREE_LIST : ([] as HostWorktree[]),
        isPlaceholderData: false,
        isError: false,
      } as ReturnType<typeof useHostWorktrees>;
    });
  }

  it("forks fresh from the project default when the last-used workspace is a worktree", async () => {
    // The most-recent workspace is a linked worktree. Without the fork-fresh
    // redirect the composer would land in it (bind mode) and never apply the
    // project's default base branch. With a default set it must instead seed
    // the MAIN repo, auto-name a branch, and fork off that default.
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [LINKED_WORKTREE] }));
    setWorktreeRepo();
    setProjectConfig({ host_id: "host_1", base_branch: "develop" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("gamma"),
    );
    const body = await submitAndReadBody();
    // Redirected to the main repo, not the linked worktree.
    expect(body.workspace).toBe(MAIN_REPO);
    const git = body.git as { branch_name: string; base_branch?: string; existing_worktree?: true };
    // A brand-new worktree (create, not a bind) forked off the project default.
    expect(git.branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
    expect(git.base_branch).toBe("develop");
    expect(git.existing_worktree).toBeUndefined();
  });

  it("keeps landing in the last-used worktree when the project has no default base branch", async () => {
    // No default base branch → the fork-fresh redirect stays off, preserving the
    // prior behavior: land directly in the recent worktree (git bind mode).
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [LINKED_WORKTREE] }));
    setWorktreeRepo();
    setProjectConfig({ host_id: "host_1" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain(
        "feature-x",
      ),
    );
    const body = await submitAndReadBody();
    // Bound straight to the worktree dir; the worktree's branch rides along and
    // no base branch is set (it's a bind, not a fork).
    expect(body.workspace).toBe(LINKED_WORKTREE);
    const git = body.git as { branch_name: string; base_branch?: string; existing_worktree?: true };
    expect(git.existing_worktree).toBe(true);
    expect(git.branch_name).toBe("feature/x");
    expect(git.base_branch).toBeUndefined();
  });

  it("does not fork-fresh when the project config supplies its own workspace", async () => {
    // The config seeds its own workspace (MAIN_REPO) even though a default base
    // branch is set and the recent path is a linked worktree. The fork-fresh
    // redirect must NOT hijack that into a worktree launch: the auto-seed is a
    // no-op on a non-empty field, so no branch is generated and the session
    // starts plainly in the configured directory.
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [LINKED_WORKTREE] }));
    setWorktreeRepo();
    setProjectConfig({ host_id: "host_1", workspace: MAIN_REPO, base_branch: "develop" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("gamma"),
    );
    const body = await submitAndReadBody();
    // Config workspace held (chip check above) → omitted for default-fill.
    expect("workspace" in body).toBe(false);
    // Plain launch — no worktree fork was manufactured from the config workspace.
    expect(body.git).toBeUndefined();
  });

  it("retracts a config-opted-in worktree branch when the project config drops it (re-opened after edit)", async () => {
    // The composer stays mounted across a pencil re-open; a stored config that
    // opts in seeds a `worktree-xxxx` branch. If the user edits the project to
    // turn the worktree default off and re-opens, the previously-seeded branch
    // must be retracted rather than lingering (the seed effect only fills an
    // empty branch and never clears on its own).
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: true });
    const { rerender } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    // Config now explicitly opts out; re-open the same project. The reseed clears
    // the composer, so the seed effect re-evaluates against the new (off) config
    // and the previously-seeded worktree branch must not come back.
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: false });
    rerender(<NewChatLandingScreen />);

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    // Config workspace held → omitted under Alpha's project_id for
    // default-fill; the retracted branch stays retracted.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
    expect(body.git).toBeUndefined();
  });

  // ── Project default model ── the stored `model` seeds the composer's model
  // pick (→ create-body model_override) while the composer sits on the
  // project's configured agent. These pin the seed itself plus the two races
  // around it: an invalid stored id must behave as "no default", and an
  // async-arriving config must not clobber a pick the user already committed.
  const CLAUDE_AGENT_ID = "ag_claude";
  const HARNESS_OPTIONS_KEY = "omnigent:last-mode-by-harness";

  function setClaudeAgentAndModels(): void {
    vi.mocked(useAvailableAgents).mockReturnValue({
      data: [
        agent(),
        agent({
          id: CLAUDE_AGENT_ID,
          name: "claude-native-ui",
          display_name: "Claude Code",
          harness: "claude-native",
        }),
      ],
    } as ReturnType<typeof useAvailableAgents>);
    vi.mocked(useHostModelOptions).mockReturnValue({
      data: [
        { id: "opus", displayName: "Opus" },
        { id: "sonnet", displayName: "Sonnet" },
      ],
    } as ReturnType<typeof useHostModelOptions>);
  }

  it("seeds the create body's model_override from the project's stored default model", async () => {
    setClaudeAgentAndModels();
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID, model: "opus" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );
    const body = await submitAndReadBody();
    // The waitFor above proves the configured agent is selected (an unchanged
    // config agent is omitted from the body for default-fill, so assert the
    // model_override contract that is the point of this test).
    expect(body.model_override).toBe("opus");
  });

  it("outranks the remembered per-harness pick with the project default", async () => {
    setClaudeAgentAndModels();
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { model: "sonnet" } }),
    );
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID, model: "opus" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );
    const body = await submitAndReadBody();
    expect(body.model_override).toBe("opus");
  });

  it("treats an invalid stored project model as no default (remembered pick still seeds)", async () => {
    // A retired/unknown id must not seed — and must not displace the user's
    // remembered pick, which stays the effective model.
    setClaudeAgentAndModels();
    localStorage.setItem(
      HARNESS_OPTIONS_KEY,
      JSON.stringify({ "claude-native": { model: "sonnet" } }),
    );
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID, model: "retired-model" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );
    const body = await submitAndReadBody();
    expect(body.model_override).toBe("sonnet");
  });

  it("does not clobber a user's committed model pick when the project config arrives late", async () => {
    // The config's model can resolve (or refresh) after the composer is
    // interactive; a pick the user already committed via the config modal
    // must survive that async arrival.
    setClaudeAgentAndModels();
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID });
    const { rerender } = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );

    // Commit "Sonnet" through the agent-config modal (the user's explicit pick).
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-config-model"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-config-model"));
    fireEvent.click(screen.getByRole("option", { name: "Sonnet" }));
    fireEvent.click(screen.getByTestId("new-chat-landing-config-save"));

    // The project default (Opus) lands afterwards — it must not reseed.
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID, model: "opus" });
    rerender(<NewChatLandingScreen />);

    const body = await submitAndReadBody();
    expect(body.model_override).toBe("sonnet");
  });

  it("lets the project default win over a landing draft that restored Smart Routing on", async () => {
    // Cross-review edge (OMNI-5841 Polly note #1): a parked landing draft can
    // restore costControlMode="on". On the remount, the project default must
    // still win — the model-seed clears the restored routing before the
    // routing-seed effect early-returns for the valid pin — so the create pins
    // the model and does NOT also send routing (which would silently drop it).
    setClaudeAgentAndModels();
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID });
    const { unmount } = renderRoutingLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );

    // Turn Smart Routing on via the config modal, then park the draft by
    // unmounting (submittedRef stays false → landingDraft keeps routing "on").
    fireEvent.click(screen.getByTestId("new-chat-landing-config-gear"));
    fireEvent.pointerDown(screen.getByTestId("new-chat-landing-config-model"), { button: 0 });
    fireEvent.click(screen.getByTestId("new-chat-landing-config-model"));
    fireEvent.click(screen.getByRole("option", { name: "Smart Routing" }));
    fireEvent.click(screen.getByTestId("new-chat-landing-config-save"));
    unmount();

    // Remount for the SAME project, now with a stored model default. The
    // restored routing draft must not shadow the pin.
    setProjectConfig({ host_id: "host_1", agent_id: CLAUDE_AGENT_ID, model: "opus" });
    renderRoutingLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-agent-select").textContent).toContain(
        "Claude Code",
      ),
    );

    const body = await submitAndReadBody();
    expect(body.model_override).toBe("opus");
    expect(body.cost_control_mode_override).not.toBe("on");
  });

  it("still seeds the recent workspace when the worktree probe errors", async () => {
    // A non-400 failure from /worktrees leaves the hook's data undefined for
    // good. The seed must fall back to the candidate as-is (treat the probe
    // error as "no redirect") rather than blocking on data that never arrives
    // and leaving the working directory blank forever.
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [LINKED_WORKTREE] }));
    vi.mocked(useHostWorktrees).mockReturnValue({
      data: undefined,
      isPlaceholderData: false,
      isError: true,
    } as ReturnType<typeof useHostWorktrees>);
    setProjectConfig({ host_id: "host_1", base_branch: "develop" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain(
        "feature-x",
      ),
    );
    const body = await submitAndReadBody();
    // Seeded the recent path as-is; no redirect, no fabricated fork.
    expect(body.workspace).toBe(LINKED_WORKTREE);
    expect(body.git).toBeUndefined();
  });
});

// The user-global "always use a worktree" default (Settings › Git, stored in
// localStorage) makes new sessions in any git workspace start in a fresh
// worktree. Precedence: a project's explicit `use_worktree` (true OR false)
// wins; an unset project falls through to this global default. These cases pin
// the full global × project matrix.
const ALWAYS_WORKTREE_KEY = "omnigent:always-use-worktree";

describe("NewChatLandingScreen global always-use-worktree default", () => {
  // The branch chip's label reflects the branch field ("Worktree" when empty),
  // so it lets a test observe the seeded/retracted branch without opening the
  // popover the actual input lives in.
  function branchLabel(): string {
    return screen.getByTestId("new-chat-landing-branch-chip").textContent ?? "";
  }

  it("seeds a worktree in a plain (non-project) git workspace when the global default is on", async () => {
    // A plain visit with no project. The recent workspace is a git repo, so the
    // global default alone drives the worktree seed.
    searchParams = new URLSearchParams("");
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [REPO] }));
    localStorage.setItem(ALWAYS_WORKTREE_KEY, "true");
    renderLanding();

    await waitFor(() => expect(branchLabel()).toMatch(/^worktree-[0-9a-f]{8}$/));
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("does NOT seed a worktree in a plain git workspace when the global default is off", async () => {
    searchParams = new URLSearchParams("");
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [REPO] }));
    // Global default unset (off).
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
    expect(body.git).toBeUndefined();
  });

  it("applies the global default to a project whose config leaves use_worktree unset", async () => {
    // Project config sets host/workspace but no worktree preference → falls
    // through to the global default (on).
    localStorage.setItem(ALWAYS_WORKTREE_KEY, "true");
    setProjectConfig({ host_id: "host_1", workspace: REPO });
    renderLanding();

    await waitFor(() => expect(branchLabel()).toMatch(/^worktree-[0-9a-f]{8}$/));
    const body = await submitAndReadBody();
    // Untouched config workspace → omitted under the project_id create; the
    // client-generated branch is always explicit.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("lets a project's explicit opt-out win over the global default (global on, project false)", async () => {
    // A project that stored `use_worktree: false` overrides the global on — no
    // worktree despite the global default.
    localStorage.setItem(ALWAYS_WORKTREE_KEY, "true");
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: false });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    // Untouched config workspace → omitted under the project_id create.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
    expect(body.git).toBeUndefined();
  });

  it("lets a project's explicit opt-in win over the global default (global off, project true)", async () => {
    // Global default off, but a project that stored `use_worktree: true` still
    // gets a worktree.
    // Global default unset (off).
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: true });
    renderLanding();

    await waitFor(() => expect(branchLabel()).toMatch(/^worktree-[0-9a-f]{8}$/));
    const body = await submitAndReadBody();
    // Untouched config workspace → omitted under the project_id create; the
    // client-generated branch is always explicit.
    expect(body.project_id).toBe("proj_alpha");
    expect("workspace" in body).toBe(false);
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("does not seed a worktree for a non-git workspace even when the global default is on", async () => {
    // The global default only applies to git repos — RECENT_WORKSPACE is not a
    // git repo (no is_main worktree), so nothing is seeded.
    searchParams = new URLSearchParams("");
    localStorage.setItem(ALWAYS_WORKTREE_KEY, "true");
    // RECENT_WORKSPACE (the default recent) is not the git REPO.
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).not.toBe(""),
    );
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.git).toBeUndefined();
  });

  it("retracts a globally-seeded worktree branch after the default is turned off and the composer remounts", async () => {
    // The reported bug: the composer preserves its state across unmount (module-
    // scoped draft) — e.g. a trip to Settings to flip the toggle — and remounts
    // from it. A branch the global default auto-seeded must not survive once the
    // default is off; it should retract to a plain launch on the next mount.
    searchParams = new URLSearchParams("");
    localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [REPO] }));
    localStorage.setItem(ALWAYS_WORKTREE_KEY, "true");

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    function Wrapper({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    const first = render(<NewChatLandingScreen />, { wrapper: Wrapper });
    await waitFor(() => expect(branchLabel()).toMatch(/^worktree-[0-9a-f]{8}$/));

    // Leave the composer (draft preserved), turn the global default off, come
    // back. The remount reads the preserved `worktree-xxxx` branch, and the
    // retraction effect must clear it now that the default is off.
    first.unmount();
    localStorage.removeItem(ALWAYS_WORKTREE_KEY);
    render(<NewChatLandingScreen />, { wrapper: Wrapper });

    await waitFor(() => expect(branchLabel()).toContain("Worktree"));
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
    expect(body.git).toBeUndefined();
  });
});
