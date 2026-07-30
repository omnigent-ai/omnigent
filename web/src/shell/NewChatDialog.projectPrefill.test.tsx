import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useProjectConfig, useProjects } from "@/hooks/useConversations";
import type { ProjectConfig } from "@/lib/projectsApi";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { NewChatLandingScreen } from "./NewChatDialog";

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
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
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
  ...(await importOriginal<typeof import("@/hooks/useConversations")>()),
  useProjects: vi.fn(),
  useProjectConfig: vi.fn(),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agentLabels")>()),
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

function renderLanding(): (ui: ReactNode) => void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  const { rerender } = render(<NewChatLandingScreen />, { wrapper: Wrapper });
  return rerender;
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
  searchParams = new URLSearchParams("project=Alpha");
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
  it("seeds host / workspace / agent from the stored config", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, agent_id: "ag_other" });
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(REPO);
    expect(body.agent_id).toBe("ag_other");
    // No opt-in worktree → no git block.
    expect(body.git).toBeUndefined();
  });

  it("creates a fresh worktree when the config opts in", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO, use_worktree: true });
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(REPO);
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("does NOT create a worktree when the config omits use_worktree", async () => {
    setProjectConfig({ host_id: "host_1", workspace: REPO });
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
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
    const rerender = renderLanding();

    // Projects finish loading → config resolves and the agent seeds.
    setProjects([{ id: "proj_alpha", name: "Alpha" }]);
    rerender(<NewChatLandingScreen />);

    const body = await submitAndReadBody();
    expect(body.agent_id).toBe("ag_other");
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
    const rerender = renderLanding();
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );

    searchParams = new URLSearchParams("project=Beta");
    rerender(<NewChatLandingScreen />);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("beta"),
    );
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(BETA_REPO);
    expect(body.agent_id).toBe("ag_other");
  });

  it("does not seed an offline config host (falls back to the generic default)", async () => {
    vi.mocked(useHosts).mockReturnValue({
      data: [host(), host({ host_id: "host_off", name: "sleepy", status: "offline" })],
    } as ReturnType<typeof useHosts>);
    setProjectConfig({ host_id: "host_off", workspace: "/somewhere" });
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
  });
});
