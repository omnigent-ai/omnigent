import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useNewestProjectSession } from "@/hooks/useConversations";
import type { Conversation } from "@/hooks/useConversations";
import { useHostWorktrees } from "@/hooks/useHostWorktrees";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { NewChatLandingScreen } from "./NewChatDialog";

// A `?project=` visit prefills the composer from the project's newest
// session (host, source repo, agent, fresh worktree branch). These tests
// pin the seeding rules and the fallbacks to the generic defaults.
const navigateMock = vi.fn();

const RECENT_KEY = "omnigent:recent-workspaces";
const RECENT_WORKSPACE = "/Users/corey/universe/src/foo";
const REPO = "/Users/corey/projects/alpha";
const WORKTREE = "/Users/corey/projects/alpha-worktrees/feature-x";

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
// The newest-session lookup is the unit under test's input — stub the hook
// itself so each case controls it without HTTP-layer plumbing.
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useConversations")>()),
  useProjects: () => ({ data: ["Alpha"] }),
  useNewestProjectSession: vi.fn(),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agentLabels")>()),
  useBrainHarnessLabels: () => ({}),
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

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_prev",
    object: "conversation",
    title: "Previous",
    created_at: 0,
    updated_at: 9,
    labels: { omni_project: "Alpha" },
    host_id: "host_1",
    workspace: REPO,
    git_branch: null,
    agent_id: "ag_hello",
    ...overrides,
  } as Conversation;
}

function setNewestSession(value: Conversation | null): void {
  vi.mocked(useNewestProjectSession).mockReturnValue({ data: value } as ReturnType<
    typeof useNewestProjectSession
  >);
}

/** Serve the repo's worktree set for any path inside it; [] elsewhere. */
function setRepoWorktrees(): void {
  const worktrees: HostWorktree[] = [
    { path: REPO, branch: "main", is_main: true, detached: false },
    { path: WORKTREE, branch: "feature-x", is_main: false, detached: false },
  ];
  vi.mocked(useHostWorktrees).mockImplementation((hostId, path) => {
    const known = hostId === "host_1" && (path === REPO || path === WORKTREE);
    return { data: known ? worktrees : [], isError: false } as ReturnType<typeof useHostWorktrees>;
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
  // A recent on the host that would win under the generic seeding rules —
  // the project prefill must beat it.
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [RECENT_WORKSPACE] }));
  setHostsAndAgents();
  setRepoWorktrees();
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
  it("seeds host, repo, agent and a fresh worktree branch from the newest session", async () => {
    setNewestSession(conversation({ agent_id: "ag_other" }));
    renderLanding();

    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(REPO);
    expect(body.agent_id).toBe("ag_other");
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("resolves a worktree-born session back to the main work tree", async () => {
    setNewestSession(conversation({ workspace: WORKTREE, git_branch: "feature-x" }));
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
    // A fresh branch, not the previous session's.
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("skips the branch when the seeded directory is not a git repo", async () => {
    setNewestSession(conversation({ workspace: "/Users/corey/notes" }));
    vi.mocked(useHostWorktrees).mockReturnValue({
      data: [] as HostWorktree[],
      isError: false,
    } as ReturnType<typeof useHostWorktrees>);
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.workspace).toBe("/Users/corey/notes");
    expect(body.git).toBeUndefined();
  });

  it("falls back to the generic defaults when the project has no sessions", async () => {
    setNewestSession(null);
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.agent_id).toBe("ag_hello");
    expect(body.git).toBeUndefined();
  });

  it("reseeds from the new project when another pencil is clicked while mounted", async () => {
    const BETA_REPO = "/Users/corey/projects/beta";
    vi.mocked(useNewestProjectSession).mockImplementation((project) => {
      const data =
        project === "Beta"
          ? conversation({ workspace: BETA_REPO, agent_id: "ag_other" })
          : conversation();
      return { data } as ReturnType<typeof useNewestProjectSession>;
    });
    vi.mocked(useHostWorktrees).mockImplementation((hostId, path) => {
      const main = path === REPO || path === BETA_REPO ? path : null;
      return {
        data:
          hostId === "host_1" && main !== null
            ? [{ path: main, branch: "main", is_main: true, detached: false }]
            : [],
        isError: false,
      } as ReturnType<typeof useHostWorktrees>;
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
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("replaces the generic auto-defaults when arriving from the plain landing page", async () => {
    setNewestSession(conversation({ agent_id: "ag_other" }));
    searchParams = new URLSearchParams();
    const rerender = renderLanding();
    // The generic defaults claim host + recent workspace first.
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
    );

    searchParams = new URLSearchParams("project=Alpha");
    rerender(<NewChatLandingScreen />);
    await waitFor(() =>
      expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("alpha"),
    );
    const body = await submitAndReadBody();
    expect(body.workspace).toBe(REPO);
    expect(body.agent_id).toBe("ag_other");
    expect((body.git as { branch_name: string }).branch_name).toMatch(/^worktree-[0-9a-f]{8}$/);
  });

  it("falls back to the generic defaults when the newest-session lookup fails", async () => {
    vi.mocked(useNewestProjectSession).mockReturnValue({
      data: undefined,
      isError: true,
    } as ReturnType<typeof useNewestProjectSession>);
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.agent_id).toBe("ag_hello");
  });

  it("falls back to the generic defaults when the session's host is gone", async () => {
    // A distinct agent on the unusable session: the WHOLE template falls
    // back, agent included — not just host and workspace.
    setNewestSession(conversation({ host_id: "host_gone", agent_id: "ag_other" }));
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.agent_id).toBe("ag_hello");
  });

  it("falls back to the generic defaults when the session's host is offline", async () => {
    // The host is still listed (the picker shows it disabled) but can't take
    // a session — the prefill must not seed it, its workspace, or its agent.
    vi.mocked(useHosts).mockReturnValue({
      data: [host(), host({ host_id: "host_off", name: "sleepy", status: "offline" })],
    } as ReturnType<typeof useHosts>);
    setNewestSession(conversation({ host_id: "host_off", agent_id: "ag_other" }));
    renderLanding();

    const body = await submitAndReadBody();
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe(RECENT_WORKSPACE);
    expect(body.agent_id).toBe("ag_hello");
  });
});
