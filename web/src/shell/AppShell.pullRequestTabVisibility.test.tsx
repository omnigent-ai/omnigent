import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Pull Requests stays reachable with a workspace or saved session associations.

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { writeWorkspacePanelDefault } from "@/lib/workspacePanelPreferences";
import { writePinnedWorkspaceTabs } from "@/lib/workspaceTabPreferences";

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useConversations: vi.fn(),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({
    data: { available: true, root: null },
    isLoading: false,
  })),
  useWorkspaceChangedFiles: vi.fn(() => ({
    data: { data: [] },
    isSuccess: true,
    isLoading: false,
  })),
}));
vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  // Keep the real module (types, the panel's sibling hooks) — only the
  // info hook AppShell reads is replaced, per-test below.
  ...(await importOriginal<typeof UsePullRequestsModule>()),
  usePullRequestInfo: vi.fn(() => ({ data: undefined, isLoading: true })),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
  useCreateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("./Sidebar", () => ({
  Sidebar: () => <div data-testid="sidebar" />,
  isMobileViewport: vi.fn(() => false),
}));
vi.mock("./PullRequestPanel", () => ({
  PullRequestPanel: () => <div data-testid="github-panel">Pull request details</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
}));
vi.mock("./FileViewer", () => ({
  FileViewer: () => <div data-testid="file-viewer" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="inline-terminals-section" />,
}));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: () => <div data-testid="files-panel-drawer" />,
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: () => <div data-testid="terminals-panel" />,
}));

import { AppShell } from "./AppShell";
import { useOpenGithubTab } from "./FileViewerContext";
import { isMobileViewport } from "./Sidebar";
import { usePullRequestInfo } from "@/hooks/usePullRequests";
import { useConversations } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import type { Session } from "@/lib/types";

const usePullRequestInfoMock = vi.mocked(usePullRequestInfo);
const savedPullRequest = {
  url: "https://github.com/acme/widget/pull/42",
  host: "github.com",
  repository: "acme/widget",
  number: 42,
  relationship: "created" as const,
};
const conversation = {
  id: "conv_ws",
  object: "conversation" as const,
  title: null,
  created_at: 0,
  updated_at: 0,
  labels: {},
  permission_level: null,
  host_id: null,
  runner_id: null,
};

afterEach(cleanup);

beforeEach(() => {
  // The rail persists per-session state (selected tab, width) in
  // localStorage; clear it so one test's writes can't leak into another.
  localStorage.clear();
  writeWorkspacePanelDefault("open");
  sessionStorage.clear();
  vi.mocked(isMobileViewport).mockReturnValue(false);
  vi.mocked(useWorkspaceEnvironment).mockReset();
  vi.mocked(useWorkspaceEnvironment).mockReturnValue({
    data: { available: true, root: null },
    isLoading: false,
  } as ReturnType<typeof useWorkspaceEnvironment>);
  vi.mocked(useSession).mockReset();
  vi.mocked(useSession).mockReturnValue({ session: null, isLoading: false, error: null });
  usePullRequestInfoMock.mockReset();
  usePullRequestInfoMock.mockReturnValue({ data: undefined, isLoading: true } as ReturnType<
    typeof usePullRequestInfo
  >);
  vi.mocked(useConversations).mockReset();
  vi.mocked(useConversations).mockReturnValue({
    data: {
      pages: [
        {
          data: [conversation],
          first_id: null,
          last_id: null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
  } as never);
});

function GithubLinkProbe() {
  const openGithubTab = useOpenGithubTab();
  return (
    <>
      <button type="button" onClick={() => openGithubTab?.()}>
        Open PR
      </button>
      <Link to="/c/conv_other">Another session</Link>
    </>
  );
}

function renderShell() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SidebarDataProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_ws"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="c/:conversationId" element={<GithubLinkProbe />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

describe("Pull Requests rail tab visibility", () => {
  it("shows the Pull Requests tab even when the workspace isn't a git repository", () => {
    usePullRequestInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: false, reason: "not_a_git_repo" },
      isLoading: false,
    } as ReturnType<typeof usePullRequestInfo>);

    renderShell();

    // The workspace gate is on: Files renders, so the strip is up — and the
    // Pull Requests tab must also be present (its panel shows an empty state instead).
    expect(screen.getByRole("tab", { name: /^Files$/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Agents/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Pull Requests" })).toBeInTheDocument();
  });

  it("shows the Pull Requests tab for a git workspace", () => {
    usePullRequestInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: true },
      isLoading: false,
    } as ReturnType<typeof usePullRequestInfo>);

    renderShell();

    expect(screen.getByRole("tab", { name: "Pull Requests" })).toBeInTheDocument();
  });

  it("keeps the Pull Requests tab when the host is outdated", () => {
    // An outdated host 404s the info endpoint (reason: host_outdated). The
    // panel renders an actionable "update your host" prompt, so the tab must
    // stay reachable rather than being hidden like the non-git dead end.
    usePullRequestInfoMock.mockReturnValue({
      data: { object: "session.github.info", available: false, reason: "host_outdated" },
      isLoading: false,
    } as ReturnType<typeof usePullRequestInfo>);

    renderShell();

    expect(screen.getByRole("tab", { name: "Pull Requests" })).toBeInTheDocument();
  });

  it("keeps the Pull Requests tab while the info is still loading (no flash)", () => {
    // Default beforeEach mock: data undefined, isLoading true — matches the
    // Files gate's optimistic default so tabs don't pop in after load.
    renderShell();

    expect(screen.getByRole("tab", { name: "Pull Requests" })).toBeInTheDocument();
  });
});

describe("opening Pull Requests from the composer", () => {
  beforeEach(() => {
    writeSessionWorkspaceState("conv_ws", { open: false, rightRailTab: "files" });
  });

  it.each([
    { viewport: "desktop", noWorkspace: true },
    { viewport: "mobile", noWorkspace: true },
    { viewport: "desktop", noWorkspace: false },
    { viewport: "mobile", noWorkspace: false },
  ])(
    "opens saved PRs on $viewport without file access (no workspace: $noWorkspace)",
    ({ viewport, noWorkspace }) => {
      vi.mocked(isMobileViewport).mockReturnValue(viewport === "mobile");
      if (noWorkspace) {
        vi.mocked(useWorkspaceEnvironment).mockReturnValue({
          data: { available: false, root: null },
          isLoading: false,
        } as ReturnType<typeof useWorkspaceEnvironment>);
        vi.mocked(useSession).mockImplementation((id) => ({
          session: id === "conv_ws" ? ({ id, pullRequests: [savedPullRequest] } as Session) : null,
          isLoading: false,
          error: null,
        }));
      } else {
        vi.mocked(useConversations).mockReturnValue({
          data: {
            pages: [
              {
                data: [
                  {
                    ...conversation,
                    permission_level: "read",
                    pull_requests: [savedPullRequest],
                  },
                ],
                first_id: null,
                last_id: null,
                has_more: false,
              },
            ],
            pageParams: [undefined],
          },
        } as never);
      }

      renderShell();
      expect(screen.queryByRole("tab", { name: /^Files$/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /^Changes$/ })).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Open PR" }));

      const target =
        viewport === "mobile"
          ? screen.getByTestId("github-panel-drawer")
          : screen.getByRole("complementary", { name: "Workspace" });
      expect(within(target).getByTestId("github-panel")).toBeInTheDocument();
    },
  );

  it("opens and dismisses the mobile drawer without opening the hidden desktop rail", () => {
    vi.mocked(isMobileViewport).mockReturnValue(true);
    renderShell();

    fireEvent.click(screen.getByRole("button", { name: "Open PR" }));

    const drawer = screen.getByTestId("github-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(within(drawer).getByTestId("github-panel")).toBeInTheDocument();
    expect(readSessionWorkspaceState("conv_ws").open).toBe(false);
    expect(screen.getAllByTestId("github-panel")).toHaveLength(1);

    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));

    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(within(drawer).queryByTestId("github-panel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open PR" })).toBeInTheDocument();
  });

  it.each([true, false])(
    "opens the desktop Pull Requests tab without mounting the mobile panel (pinned: %s)",
    (isPinned) => {
      if (!isPinned) writePinnedWorkspaceTabs(["files", "changes", "subagents"]);
      renderShell();

      fireEvent.click(screen.getByRole("button", { name: "Open PR" }));

      const workspace = screen.getByRole("complementary", { name: "Workspace" });
      expect(within(workspace).getByRole("tab", { name: "Pull Requests" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(within(workspace).getByTestId("github-panel")).toBeInTheDocument();
      expect(
        within(workspace).queryAllByRole("button", { name: "Close Pull Requests" }),
      ).toHaveLength(isPinned ? 0 : 1);
      expect(readSessionWorkspaceState("conv_ws")).toMatchObject({
        open: true,
        rightRailTab: "github",
        openPanels: isPinned ? [] : ["github"],
      });
      const drawer = screen.getByTestId("github-panel-drawer");
      expect(drawer).toHaveAttribute("data-state", "closed");
      expect(within(drawer).queryByTestId("github-panel")).not.toBeInTheDocument();
    },
  );

  it("closes the mobile Pull Requests drawer when navigating to another session", () => {
    vi.mocked(isMobileViewport).mockReturnValue(true);
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Open PR" }));
    expect(screen.getByTestId("github-panel-drawer")).toHaveAttribute("data-state", "open");

    fireEvent.click(screen.getByRole("link", { name: "Another session" }));

    expect(screen.getByTestId("github-panel-drawer")).toHaveAttribute("data-state", "closed");
  });
});
