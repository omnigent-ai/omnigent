import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as FileViewerContextModule from "@/shell/FileViewerContext";
import type { GithubInfo } from "@/hooks/useGithub";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";

// Same stub set as ChatPage.statusLine.test.tsx: the composer reads workspace
// files and GitHub info through TanStack query hooks; stubbing them keeps
// these renders free of a QueryClientProvider.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => useGithubInfoMock(),
}));
vi.mock("@/shell/FileViewerContext", async (importOriginal) => ({
  ...(await importOriginal<typeof FileViewerContextModule>()),
  useOpenGithubTab: () => openGithubTabMock,
}));

const {
  useSessionMock,
  useHostsMock,
  useSessionHostOnlineMock,
  useGithubInfoMock,
  openGithubTabMock,
} = vi.hoisted(() => ({
  useSessionMock: vi.fn(),
  useHostsMock: vi.fn(),
  useSessionHostOnlineMock: vi.fn(),
  useGithubInfoMock: vi.fn(),
  openGithubTabMock: vi.fn(),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: (id: string | null | undefined) => useSessionMock(id),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: (opts: unknown) => useHostsMock(opts),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: (id: string | undefined) => useSessionHostOnlineMock(id),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
}));

import { Composer, composerBranchChip } from "./ChatPage";

// Pins the composer branch chip's data sources. The runner reports the
// workspace's live branch through /resources/github; the chip must show that
// value (the create-time persisted git_branch is only set for worktree
// sessions, so a plain git-checkout session would otherwise read
// "No branch reported" forever). The persisted branch stays as the fallback
// while the runner hasn't reported, and each empty state names what the
// workspace actually is (detached HEAD vs. not a git repo).

/** Minimal ComposerProps for an interactive (writable, idle) composer. */
function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    selectedAgentId: null,
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function renderComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return render(
    <TooltipProvider>
      <Composer {...composerProps(overrides)} />
    </TooltipProvider>,
  );
}

function chip(): HTMLElement {
  return screen.getByTestId("composer-git-branch");
}

function openChipPopover() {
  fireEvent.pointerDown(chip(), { button: 0 });
}

function githubData(data: Partial<GithubInfo> | undefined) {
  useGithubInfoMock.mockReturnValue({
    data: data ? ({ object: "session.github.info", ...data } as GithubInfo) : undefined,
  });
}

describe("composerBranchChip", () => {
  it("uses the live-reported branch, over any persisted one", () => {
    const info = { object: "session.github.info", available: true, branch: "feat/x" } as GithubInfo;
    expect(composerBranchChip(info, null).label).toBe("feat/x");
    expect(composerBranchChip(info, "recorded-at-create").label).toBe("feat/x");
  });

  it("names a detached HEAD instead of claiming no report", () => {
    const info = { object: "session.github.info", available: true, branch: "HEAD" } as GithubInfo;
    expect(composerBranchChip(info, null)).toEqual({
      label: "Detached HEAD",
      detail: "The workspace is on a detached HEAD — no branch is checked out.",
    });
  });

  it("names a non-git workspace instead of claiming no report", () => {
    const info = {
      object: "session.github.info",
      available: false,
      reason: "not_a_git_repo",
    } as GithubInfo;
    expect(composerBranchChip(info, null)).toEqual({
      label: "Not a git repository",
      detail: "The session workspace is not a git repository.",
    });
  });

  it("falls back to the persisted branch while the runner hasn't reported", () => {
    // No info at all (runner offline / query not resolved) and an unreadable
    // workspace (no_os_env / host_outdated) both keep the persisted value —
    // only a definite not_a_git_repo may override it.
    expect(composerBranchChip(undefined, "wt-branch").label).toBe("wt-branch");
    const unreadable = {
      object: "session.github.info",
      available: false,
      reason: "no_os_env",
    } as GithubInfo;
    expect(composerBranchChip(unreadable, "wt-branch").label).toBe("wt-branch");
  });

  it("reserves 'No branch reported' for a genuinely unreported branch", () => {
    expect(composerBranchChip(undefined, null)).toEqual({
      label: "No branch reported",
      detail: "The runner has not reported a branch for this session.",
    });
  });
});

describe("Composer branch chip (live runner-reported branch)", () => {
  beforeEach(() => {
    useSessionMock.mockReset().mockReturnValue({
      session: { hostId: null },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReset().mockReturnValue({ data: [] });
    useSessionHostOnlineMock.mockReset().mockReturnValue(undefined);
    useGithubInfoMock.mockReset().mockReturnValue({ data: undefined });
    openGithubTabMock.mockReset();
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      contextWindow: null,
      tokensUsed: null,
      sessionCostUsd: null,
      gitBranch: null,
      llmModel: null,
      selectedModel: null,
      selectedEffort: null,
      codexModelOptions: [],
      codexPlanMode: false,
      nativeVendorOwnsModel: false,
      sessionHarness: null,
      subAgentName: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the runner-reported branch when no branch was persisted at create", () => {
    // The bug: a plain git-checkout session (no git options on create) has a
    // null persisted git_branch, and the chip stayed at "No branch reported"
    // even though the runner reported the checked-out branch.
    githubData({ available: true, branch: "codex/fix-resume-mcp-startup-events" });
    renderComposer();
    expect(chip()).toHaveTextContent("codex/fix-resume-mcp-startup-events");
  });

  it("prefers the live branch over a stale persisted one", () => {
    useChatStore.setState({ gitBranch: "recorded-at-create" });
    githubData({ available: true, branch: "switched-to-later" });
    renderComposer();
    expect(chip()).toHaveTextContent("switched-to-later");
  });

  it("keeps the persisted worktree branch while the runner is unreachable", () => {
    useChatStore.setState({ gitBranch: "wt-branch" });
    githubData(undefined);
    renderComposer();
    expect(chip()).toHaveTextContent("wt-branch");
  });

  it("labels a detached HEAD as such, with popover copy naming the state", () => {
    githubData({ available: true, branch: "HEAD" });
    renderComposer();
    expect(chip()).toHaveTextContent("Detached HEAD");
    openChipPopover();
    expect(
      screen.getByText("The workspace is on a detached HEAD — no branch is checked out."),
    ).toBeInTheDocument();
  });

  it("labels a non-git workspace as such", () => {
    githubData({ available: false, reason: "not_a_git_repo" });
    renderComposer();
    expect(chip()).toHaveTextContent("Not a git repository");
  });

  it("titles the popover in branch terms and omits the worktree section for a plain checkout", () => {
    githubData({ available: true, branch: "main" });
    renderComposer();
    openChipPopover();
    expect(screen.getByText("Session branch")).toBeInTheDocument();
    expect(screen.queryByText("Session worktree")).toBeNull();
  });

  it("lists the worktree path in the popover for a session-scoped worktree", () => {
    // A persisted git_branch marks a session worktree; its path (the session
    // workspace) and the branch each get their own popover section, so the
    // two facts no longer compete for the chip's one label.
    useSessionMock.mockReturnValue({
      session: { hostId: null, workspace: "/repos/omnigent-wt/wt-dir" },
      isLoading: false,
      error: null,
    });
    useChatStore.setState({ gitBranch: "feature-x" });
    githubData({ available: true, branch: "feature-x" });
    renderComposer();
    expect(chip()).toHaveTextContent("feature-x");
    openChipPopover();
    expect(screen.getByText("Session branch")).toBeInTheDocument();
    expect(screen.getByText("Session worktree")).toBeInTheDocument();
    expect(screen.getByText("/repos/omnigent-wt/wt-dir")).toBeInTheDocument();
  });
});
