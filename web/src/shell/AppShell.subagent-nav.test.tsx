import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Regression test for the "click sub-agent → rail tab jumps to Files" bug.
//
// When the user clicks a sub-agent row in the right rail, the navigation
// would briefly resolve AppShell's ``rootSessionId`` to the new child id
// (because ``activeSession`` lags one render while its snapshot loads).
// During that flicker ``useChildSessions(child_id)`` returns empty, so
// ``showSubagentsTab`` flipped false, the tab disappeared, and the
// tab-validity effect yanked the user off Agents onto Files.
//
// The fix makes ``rootSessionId`` sticky: AppShell remembers the last
// resolved root, and navigating into a *known member of that root's
// cached tree* (per ``cachedTreeContains``) holds the root (and thus
// the child list, and the Agents tab) steady until the target's
// snapshot and root walk resolve. This test exercises that path — the
// child id IS a known child of the root being viewed, so the rail must
// stay on Agents.
//
// This test renders AppShell with the REAL SubagentsPanel (not the
// mock used by AppShell.test.tsx) so we can drive a real ``<Link>``
// click through the router and observe the resulting rail-tab state.

import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseConversationsModule from "@/hooks/useConversations";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { subagentTabId } from "@/lib/subagentTabs";
import type { Session } from "@/lib/types";
import { writeWorkspacePanelDefault } from "@/lib/workspacePanelPreferences";

// Match the AppShell.test.tsx mocks except DO NOT mock SubagentsPanel —
// we want the real one so its <Link> renders.
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
// AppShell reads the GitHub info to gate the rail's GitHub tab; keep it
// loading here so no network-backed query fires.
vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfo: vi.fn(() => ({ data: undefined, isLoading: true })),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  // Keep the real module — childSessionsQueryKey, MAX_TREE_DEPTH, and
  // cachedTreeContains (which reads the query cache seeded below) stay
  // genuine; only the hook is replaced.
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  // useRootSessionId stays real: with the snapshot mocked to a
  // top-level session it resolves without fetching, and during the
  // navigation race (parentSessionId undefined) it stays null, which
  // is exactly the production behavior the sticky root rides out.
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
  isMobileViewport: () => false,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel" />,
}));
vi.mock("./FileViewer", () => ({
  FileViewer: ({ open, path, frameless }: { open: boolean; path: string; frameless?: boolean }) => (
    <div
      data-testid={frameless ? "file-viewer-inline" : "file-viewer"}
      data-state={open ? "open" : "closed"}
      data-path={path}
    />
  ),
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
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="claude" />,
}));
// A sub-agent tab's transcript is covered by SideChatPane's own suite.
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({ childId }: { childId: string }) => (
    <div data-testid="side-chat-pane-stub">{childId}</div>
  ),
}));
vi.mock("@/components/icons/NessieIcon", () => ({
  NessieIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="nessie" />,
}));

import { AppShell } from "./AppShell";
import { SubagentActivityMessage } from "@/components/blocks/SubagentActivityMessage";
import { childSessionsQueryKey, useChildSessions } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { useConversations } from "@/hooks/useConversations";

afterEach(cleanup);

beforeEach(() => {
  // The rail's open-state persists per session in localStorage; clear it so
  // state written by one test (e.g. a collapse) can't leak into the next.
  localStorage.clear();
  writeWorkspacePanelDefault("open");
  vi.mocked(useConversations).mockReset();
  vi.mocked(useConversations).mockReturnValue({
    data: {
      pages: [
        {
          data: [
            {
              id: "conv_root",
              object: "conversation" as const,
              title: null,
              created_at: 0,
              updated_at: 0,
              labels: {},
              permission_level: null,
              host_id: null,
              runner_id: null,
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
});

function PathProbe() {
  return <div data-testid="page">{useLocation().pathname}</div>;
}

describe("click sub-agent in rail (real SubagentsPanel)", () => {
  it("keeps the Agents tab and rail width across root, child, and sibling navigation", async () => {
    const children = [
      {
        id: "conv_child",
        title: null,
        task_summary: null,
        tool: "researcher",
        session_name: null,
        current_task_status: null,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
      {
        id: "conv_sibling",
        title: null,
        task_summary: null,
        tool: "researcher",
        session_name: null,
        current_task_status: null,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
    ];
    // Setup: parent has two child sub-agents. User starts on parent
    // with the Agents tab selected; URL has ?file=foo.txt (stale from
    // a previous file-viewer interaction).
    //
    // Crucially: simulate the real query-key-change behavior. When
    // ``rootSessionId`` flips to the child id (briefly, while
    // activeSession's parentSessionId snapshot loads), the new
    // queryKey returns isLoading=true, children=[]. The hook returns
    // empty children for unknown ids. With the sticky fix, conv_root
    // stays the resolved root so only conv_root is ever queried; the
    // empty-children branch below is what would collapse the Agents tab
    // if the code regressed to querying the child id — i.e. it's what
    // lets this test catch such a regression.
    vi.mocked(useChildSessions).mockImplementation((id) => {
      if (id === "conv_root") {
        return {
          children,
          isLoading: false,
          error: null,
        };
      }
      // Any non-root id (a regression would query conv_child here)
      // returns empty + loading.
      return { children: [], isLoading: true, error: null };
    });
    vi.mocked(useSession).mockImplementation((id) => {
      if (id === "conv_root") {
        return {
          session: {
            id: "conv_root",
            agentId: "ag",
            agentName: null,
            runnerId: null,
            status: "idle",
            createdAt: 0,
            title: null,
            labels: {},
            items: [],
            pendingElicitations: [],
            permissionLevel: 4,
            parentSessionId: null,
          },
          isLoading: false,
          error: null,
        } as never;
      }
      // Simulate the real-world race: when the user navigates to a child,
      // ``useSession(conv_child)`` is loading on first render. Without the
      // sticky root, the fallback would briefly resolve to the child itself.
      if (id === "conv_child" || id === "conv_sibling") {
        return { session: null, isLoading: true, error: null } as never;
      }
      return { session: null, isLoading: false, error: null };
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed the child-sessions cache the way the rendered rail does in
    // production (its useChildSessions queries write here). The sticky
    // root holds across the parent→child click only while the target is
    // a known member of the last root's cached tree.
    qc.setQueryData(childSessionsQueryKey("conv_root"), children);
    writeSessionWorkspaceState("conv_root", { widthPx: 500 });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_root?file=foo.txt"]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route path="c/:conversationId" element={<div data-testid="page" />} />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    // Switch to Agents tab. Radix tabs activate on the full
    // pointerdown→mousedown→mouseup→click sequence in jsdom; missing
    // any one of these leaves the tab in its prior state.
    const agentsTab = screen
      .getAllByRole("tablist")
      .map((tablist) => within(tablist).queryByRole("tab", { name: /Agents/i }))
      .find((tab): tab is HTMLElement => tab !== null);
    if (!agentsTab) throw new Error("Agents tab was not rendered");
    fireEvent.pointerDown(agentsTab, { button: 0 });
    fireEvent.mouseDown(agentsTab, { button: 0 });
    fireEvent.mouseUp(agentsTab, { button: 0 });
    fireEvent.click(agentsTab);

    // Sanity: the Agents tab must actually be selected before we click in.
    expect(agentsTab).toHaveAttribute("aria-selected", "true");

    // SubagentsPanel renders now that the tab is selected.
    const childRows = screen.getAllByTestId("subagent-row");
    expect(childRows).toHaveLength(2);
    expect(childRows[0]).toHaveAttribute("href", "/c/conv_child");
    expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveStyle({
      width: "500px",
    });
    fireEvent.click(childRows[0]);

    // After navigation: the same rail Agents tab we clicked must STILL be
    // mounted and selected, not just any matching tab elsewhere in the shell.
    expect(agentsTab.isConnected).toBe(true);
    expect(agentsTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveStyle({
      width: "500px",
    });

    fireEvent.click(screen.getAllByTestId("subagent-row")[1]);
    expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveStyle({
      width: "500px",
    });

    fireEvent.click(screen.getByTestId("subagent-main-row"));
    expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveStyle({
      width: "500px",
    });
  });

  it.each([undefined, "files"] as const)(
    "returns from a deep-linked child to a root with remembered tab %s",
    (rememberedTab) => {
      vi.mocked(useChildSessions).mockReturnValue({ children: [], isLoading: false, error: null });
      vi.mocked(useSession).mockImplementation((id) => ({
        session: id
          ? {
              id,
              agentId: "ag",
              agentName: null,
              runnerId: null,
              status: "idle",
              createdAt: 0,
              title: null,
              labels: {},
              items: [],
              pendingElicitations: [],
              permissionLevel: 4,
              parentSessionId: id === "conv_child" ? "conv_root" : null,
              subAgentName: null,
              kind: id === "conv_child" ? "sub_agent" : "default",
            }
          : null,
        isLoading: false,
        error: null,
      }));
      writeSessionWorkspaceState("conv_child", { rightRailTab: "subagents" });
      if (rememberedTab) writeSessionWorkspaceState("conv_root", { rightRailTab: rememberedTab });
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      qc.setQueryData(["rootSessionId", "conv_child"], "conv_root");
      render(
        <QueryClientProvider client={qc}>
          <SidebarDataProvider>
            <TooltipProvider>
              <MemoryRouter initialEntries={["/c/conv_child"]}>
                <Routes>
                  <Route element={<AppShell />}>
                    <Route path="c/:conversationId" element={<div data-testid="page" />} />
                  </Route>
                </Routes>
              </MemoryRouter>
            </TooltipProvider>
          </SidebarDataProvider>
        </QueryClientProvider>,
      );
      expect(screen.getByRole("tab", { name: /Agents/i })).toHaveAttribute("aria-selected", "true");
      const mainRow = screen.getByTestId("subagent-main-row");
      expect(mainRow).toHaveAttribute("href", "/c/conv_root");

      fireEvent.click(mainRow);

      const expectedTab = rememberedTab ? /Files/i : /Agents/i;
      expect(screen.getByRole("tab", { name: expectedTab })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(readSessionWorkspaceState("conv_root").rightRailTab).toBe(
        rememberedTab ?? "subagents",
      );
    },
  );

  /** Render `path` in a real shell over root → auth (→ tokens) and billing, with Agents open. */
  function renderSubagentTree(path: string) {
    const summary = (id: string, name: string) => ({
      id,
      title: `researcher:${name}`,
      task_summary: null,
      tool: "researcher",
      session_name: name,
      current_task_status: null,
      busy: false,
      last_message_preview: null,
      pending_elicitations_count: 0,
    });
    const tree: Record<string, ReturnType<typeof summary>[]> = {
      conv_root: [summary("conv_child", "auth"), summary("conv_sibling", "billing")],
      conv_child: [summary("conv_grandchild", "tokens")],
      conv_grandchild: [summary("conv_greatgrandchild", "expiry")],
    };
    vi.mocked(useChildSessions).mockImplementation((id) => ({
      children: tree[id ?? ""] ?? [],
      isLoading: false,
      error: null,
    }));
    const snapshot = (id: string, parentSessionId: string | null, title: string | null) =>
      ({
        id,
        agentId: "ag",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title,
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId,
        subAgentName: null,
        kind: parentSessionId === null ? "default" : "sub_agent",
      }) satisfies Session;
    const sessions: Record<string, Session> = {
      conv_root: snapshot("conv_root", null, null),
      conv_child: snapshot("conv_child", "conv_root", "researcher:auth"),
      conv_sibling: snapshot("conv_sibling", "conv_root", "researcher:billing"),
      conv_grandchild: snapshot("conv_grandchild", "conv_child", "researcher:tokens"),
      conv_greatgrandchild: snapshot(
        "conv_greatgrandchild",
        "conv_grandchild",
        "researcher:expiry",
      ),
    };
    vi.mocked(useSession).mockImplementation((id) => ({
      session: sessions[id ?? ""] ?? null,
      isLoading: false,
      error: null,
    }));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    for (const [id, children] of Object.entries(tree)) {
      qc.setQueryData(childSessionsQueryKey(id), children);
    }
    for (const [id, session] of Object.entries(sessions)) qc.setQueryData(["session", id], session);
    writeSessionWorkspaceState(path.split("?")[0].slice("/c/".length), {
      rightRailTab: "subagents",
    });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={[path]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route
                    path="c/:conversationId"
                    element={
                      <>
                        <PathProbe />
                        <SubagentActivityMessage
                          data={{
                            type: "resource_event",
                            event_type: "session.subagent.delegated",
                            resource_type: "session",
                            resource_id: "conv_child",
                            resource: { title: "Audit auth" },
                          }}
                        />
                      </>
                    }
                  />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );
    return qc;
  }

  /** The Agents-tab row for `childId`, once the rail has rooted its tree. */
  const agentRow = (childId: string) =>
    waitFor(() => {
      const row = screen
        .getAllByTestId("subagent-row")
        .find((candidate) => candidate.getAttribute("data-child-session-id") === childId);
      expect(row).toBeDefined();
      return row!;
    });

  it.each([
    {
      entry: "an Agents-tab row",
      path: "/c/conv_root",
      prepare: () => {},
      click: async () => fireEvent.click(await agentRow("conv_child")),
    },
    {
      entry: "a transcript link while execution logs cover the rail",
      path: "/c/conv_root?debug=1",
      prepare: () => {
        fireEvent.click(screen.getByTestId("execution-log-row-main"));
        expect(screen.getByTestId("execution-logs-panel")).toHaveAttribute("data-state", "open");
      },
      click: async () => fireEvent.click(screen.getByRole("link", { name: "Audit auth" })),
    },
  ])(
    "opens a sub-agent from $entry as a rail tab beside the parent",
    async ({ path, prepare, click }) => {
      writeOpenSubagentsInPanel(true);
      renderSubagentTree(path);
      prepare();

      await click();

      expect(screen.getByTestId("page")).toHaveTextContent("/c/conv_root");
      // The opener's revealRightPanel closes panels that cover the rail.
      expect(screen.queryByTestId("execution-logs-panel")).not.toHaveAttribute(
        "data-state",
        "open",
      );
      const rail = screen.getByRole("complementary", { name: "Workspace" });
      expect(within(rail).getByRole("tab", { name: "auth" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(within(rail).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_child");
    },
  );

  it("opens a sub-agent tab's full screen link on the Agents tab", async () => {
    writeOpenSubagentsInPanel(true);
    renderSubagentTree("/c/conv_root");
    fireEvent.click(await agentRow("conv_child"));

    const rail = screen.getByRole("complementary", { name: "Workspace" });
    fireEvent.click(within(rail).getByRole("link", { name: "Open full screen" }));

    expect(screen.getByTestId("page")).toHaveTextContent("/c/conv_child");
    await waitFor(() =>
      expect(within(rail).getByRole("tab", { name: /^Agents/ })).toHaveAttribute(
        "aria-selected",
        "true",
      ),
    );
    // Remembered, so later clicks on this sub-agent from the Agents tab stay there.
    expect(readSessionWorkspaceState("conv_child").rightRailTab).toBe("subagents");
  });

  it.each([
    {
      name: "navigates to an ancestor instead of opening it as a tab",
      page: "conv_grandchild",
      clicked: "conv_child",
      tabs: undefined,
    },
    {
      name: "navigates to an ancestor whose descendants' snapshots aren't loaded",
      page: "conv_greatgrandchild",
      clicked: "conv_child",
      tabs: undefined,
      uncached: "conv_grandchild",
    },
    {
      name: "opens a sibling as a tab and stays on the chat on screen",
      page: "conv_child",
      clicked: "conv_sibling",
      tabs: [subagentTabId("conv_sibling")],
    },
  ])("$name", async ({ page, clicked, tabs, uncached }) => {
    writeOpenSubagentsInPanel(true);
    const qc = renderSubagentTree(`/c/${page}`);
    const row = await agentRow(clicked);
    // The parent chain can't be walked past a snapshot that hasn't loaded.
    if (uncached) qc.removeQueries({ queryKey: ["session", uncached] });

    fireEvent.click(row);

    expect(screen.getByTestId("page")).toHaveTextContent(
      tabs === undefined ? `/c/${clicked}` : `/c/${page}`,
    );
    expect(readSessionWorkspaceState(page).openSubagentTabs).toEqual(tabs);
  });

  it("restores a saved width while a cold-loaded session snapshot is unresolved", () => {
    vi.mocked(useChildSessions).mockReturnValue({
      children: [],
      isLoading: true,
      error: null,
    });
    vi.mocked(useSession).mockReturnValue({
      session: null,
      isLoading: true,
      error: null,
    } as never);
    writeSessionWorkspaceState("conv_cold", { widthPx: 500 });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_cold"]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route path="c/:conversationId" element={<div data-testid="page" />} />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveStyle({
      width: "500px",
    });
  });

  it("does not persist width under a cold-loaded child's tentative key", () => {
    vi.mocked(useChildSessions).mockReturnValue({
      children: [],
      isLoading: true,
      error: null,
    });
    vi.mocked(useSession).mockReturnValue({
      session: null,
      isLoading: true,
      error: null,
    } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_cold_child"]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route path="c/:conversationId" element={<div data-testid="page" />} />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    fireEvent.keyDown(screen.getByRole("separator", { name: "Resize panel" }), {
      key: "ArrowLeft",
    });
    expect(readSessionWorkspaceState("conv_cold_child").widthPx).toBeUndefined();
  });

  it("shows the Agents tab with count 1 while a childless session's initial fetch is loading", () => {
    // The Agents tab is unconditional and its badge counts the whole
    // tree including the main agent, so a childless session — even
    // mid-fetch — reads "Agents 1" with no flicker (the count can only
    // grow once children land). "0" or a missing tab means the main
    // agent was dropped from the count or the always-visible rule
    // regressed.
    vi.mocked(useChildSessions).mockReturnValue({
      children: [],
      isLoading: true,
      error: null,
    });
    vi.mocked(useSession).mockReturnValue({
      session: {
        id: "conv_solo",
        agentId: "ag",
        agentName: null,
        runnerId: null,
        status: "idle",
        createdAt: 0,
        title: null,
        labels: {},
        items: [],
        pendingElicitations: [],
        permissionLevel: 4,
        parentSessionId: null,
      },
      isLoading: false,
      error: null,
    } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SidebarDataProvider>
          <TooltipProvider>
            <MemoryRouter initialEntries={["/c/conv_solo"]}>
              <Routes>
                <Route element={<AppShell />}>
                  <Route path="c/:conversationId" element={<div data-testid="page" />} />
                </Route>
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    // The tab is present with the main-agent-only count; Files stays
    // the default selection.
    const agentsTab = screen.getByRole("tab", { name: /Agents\s*1/i });
    expect(agentsTab).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "true");
  });
});
