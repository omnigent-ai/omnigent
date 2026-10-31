import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type * as FileViewerContextModule from "@/shell/FileViewerContext";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions). These slash-command tests don't exercise that, so stub the hook
// to avoid needing a QueryClientProvider around every bare render.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});

// ComposerStatusLine's PR link reads GitHub info via a TanStack query; stub it
// (default: no PR) so bare Composer renders don't need a QueryClientProvider.
vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfo: () => ({ data: undefined }),
}));
// The workspace bar's git-status hook uses TanStack Query; stub it so the
// composer renders in isolation (no QueryClient) with a neutral empty status.
// The hoisted spy records the args so a test can assert the page passes the
// real session id / host / workspace / creation branch (not fixtures).
const { composerGitStatusArgsSpy, composerGitStatusSnapshot } = vi.hoisted(() => ({
  composerGitStatusArgsSpy: vi.fn(),
  composerGitStatusSnapshot: {
    branch: null as string | null,
    branchState: "unknown" as "loading" | "branch" | "detached" | "not-git" | "unknown",
    isWorktree: null as boolean | null,
    worktreePath: null as string | null,
    creationBranch: null as string | null,
    repoNameWithOwner: "omnigent-ai/omnigent" as string | null,
    githubState: "ready" as "loading" | "ready" | "unknown",
    prCount: 0,
    prNumber: null as number | null,
    prNumberPrefix: "#",
    refresh: vi.fn(),
    refreshing: false,
  },
}));
const { openGithubTabMock } = vi.hoisted(() => ({ openGithubTabMock: vi.fn() }));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: (args: unknown) => {
    composerGitStatusArgsSpy(args);
    return composerGitStatusSnapshot;
  },
}));
vi.mock("@/shell/FileViewerContext", async (importOriginal) => ({
  ...(await importOriginal<typeof FileViewerContextModule>()),
  useOpenGithubTab: () => openGithubTabMock,
}));

function setComposerGitStatus(overrides: Record<string, unknown> = {}) {
  Object.assign(
    composerGitStatusSnapshot,
    {
      branch: null,
      branchState: "unknown",
      isWorktree: null,
      worktreePath: null,
      creationBranch: null,
      repoNameWithOwner: "omnigent-ai/omnigent",
      githubState: "ready",
      prCount: 0,
      prNumber: null,
      prNumberPrefix: "#",
      refreshing: false,
    },
    overrides,
  );
}

afterEach(() => setComposerGitStatus());
// SubagentTaskIndicator's child-session query also needs a QueryClient; stub it
// so the indicator self-hides (no active children) in isolated composer renders.
const { childSessionsArgsSpy, composerChildSessions } = vi.hoisted(() => ({
  childSessionsArgsSpy: vi.fn(),
  composerChildSessions: { children: [] as ChildSessionInfo[] },
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: (conversationId: string | null) => {
    childSessionsArgsSpy(conversationId);
    return { children: composerChildSessions.children, isLoading: false, error: null };
  },
}));
// HostBadge now renders in the composer's status-line tray and reads the
// session's host binding via TanStack Query. Stub the hooks so it self-hides
// (no host bound) without needing a QueryClient provider around these renders.
const { composerSessionSnapshot } = vi.hoisted(() => ({
  composerSessionSnapshot: {
    hostId: null as string | null,
    workspace: null as string | null,
    labels: {} as Record<string, string>,
    gitBranch: null as string | null,
  },
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({
    session: composerSessionSnapshot,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import type { ComponentProps } from "react";
import { MainAgentSurface, composerCannotRestartSession } from "./ChatPage";

// The asleep notice must follow the same send gates the composer applies.
// `MainAgentSurface` threads them into `ConnectionIndicator`, so render it for
// real instead of only testing the indicator prop.

afterEach(cleanup);

function surfaceProps(
  overrides: Partial<ComponentProps<typeof MainAgentSurface>> = {},
): ComponentProps<typeof MainAgentSurface> {
  return {
    conversationId: "conv_asleep_surface",
    hostId: "host_1",
    status: "idle",
    isWorking: false,
    showsWorking: false,
    runnerOnline: false,
    liveness: { kind: "runner_asleep" },
    agentsError: null,
    disabled: false,
    onSend: vi.fn(),
    onNewSession: vi.fn(),
    onStop: vi.fn(),
    onShowReconnectHelp: vi.fn(),
    agents: undefined,
    selectedAgentId: "agent_1",
    hasMoreHistory: false,
    loadingMoreHistory: false,
    permissionLevel: 4,
    readOnlyReason: null,
    sendDisabledReason: null,
    effortLevels: [],
    showEffort: false,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    costRoutingEligible: false,
    subagentRoutingEligible: false,
    subAgentLabel: null,
    wrapperLabel: null,
    ...overrides,
  };
}

function renderSurface(overrides: Partial<ComponentProps<typeof MainAgentSurface>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <MainAgentSurface {...surfaceProps(overrides)} />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("MainAgentSurface asleep notice threading", () => {
  it("shows the notice for a writable owner session", () => {
    renderSurface();
    expect(screen.getByTestId("asleep-indicator")).toBeInTheDocument();
  });

  it("hides it when the composer is disabled (no agent / agents failed to load)", () => {
    renderSurface({ disabled: true });
    expect(screen.queryByTestId("asleep-indicator")).toBeNull();
  });

  it("shows it when the permission level is null (single-user: full access)", () => {
    renderSurface({ permissionLevel: null });
    expect(screen.getByTestId("asleep-indicator")).toBeInTheDocument();
  });

  it("hides it when sending is disabled by a reason", () => {
    renderSurface({ sendDisabledReason: "Waiting for approval" });
    expect(screen.queryByTestId("asleep-indicator")).toBeNull();
  });

  it("hides it for a read-only viewer and a structurally read-only session", () => {
    renderSurface({ permissionLevel: 1 });
    expect(screen.queryByTestId("asleep-indicator")).toBeNull();
    cleanup();
    renderSurface({ readOnlyReason: "Imported transcript" });
    expect(screen.queryByTestId("asleep-indicator")).toBeNull();
  });
});

describe("composerCannotRestartSession", () => {
  const writable = {
    readOnlyReason: null,
    permissionLevel: 4,
    sendDisabledReason: null,
    disabled: false,
  };

  it("is false for a writable, enabled composer, including a null (single-user) level", () => {
    expect(composerCannotRestartSession(writable)).toBe(false);
    expect(composerCannotRestartSession({ ...writable, permissionLevel: null })).toBe(false);
    expect(composerCannotRestartSession({ ...writable, permissionLevel: 2 })).toBe(false);
  });

  it.each([
    { disabled: true },
    { permissionLevel: 1 },
    { sendDisabledReason: "x" },
    { readOnlyReason: "x" },
  ])("is true for %o", (override) => {
    expect(composerCannotRestartSession({ ...writable, ...override })).toBe(true);
  });
});
