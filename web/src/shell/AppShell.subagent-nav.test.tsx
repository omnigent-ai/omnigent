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

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Match the AppShell.test.tsx mocks except DO NOT mock SubagentsPanel —
// we want the real one so its <Link> renders.
vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useStopSession: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useTerminals")>()),
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
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  // Keep the real module — childSessionsQueryKey, MAX_TREE_DEPTH, and
  // cachedTreeContains (which reads the query cache seeded below) stay
  // genuine; only the hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useChildSessions")>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  // useRootSessionId stays real: with the snapshot mocked to a
  // top-level session it resolves without fetching, and during the
  // navigation race (parentSessionId undefined) it stays null, which
  // is exactly the production behavior the sticky root rides out.
  ...(await importOriginal<typeof import("@/hooks/useSession")>()),
  useSession: vi.fn(() => ({ session: null, isLoading: false, error: null })),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
  useCreateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useUpdateMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useDeleteMcpServer: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));
vi.mock("./Sidebar", () => ({ Sidebar: () => <div data-testid="sidebar" /> }));
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
vi.mock("./TodoPanel", () => ({ TodoPanel: () => <div data-testid="todo-panel" /> }));
vi.mock("./FilesPanelDrawer", () => ({
  FilesPanelDrawer: () => <div data-testid="files-panel-drawer" />,
}));
vi.mock("./TerminalsPanel", () => ({
  TerminalsPanel: () => <div data-testid="terminals-panel" />,
}));
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="claude" />,
}));
vi.mock("@/components/icons/NessieIcon", () => ({
  NessieIcon: (props: Record<string, unknown>) => <svg {...props} data-icon="nessie" />,
}));

import { AppShell } from "./AppShell";
import { childSessionsQueryKey, useChildSessions } from "@/hooks/useChildSessions";
import { useSession } from "@/hooks/useSession";
import { useConversations } from "@/hooks/useConversations";

afterEach(cleanup);

beforeEach(() => {
  // The rail's open-state persists per session in localStorage; clear it so
  // state written by one test (e.g. a collapse) can't leak into the next.
  localStorage.clear();
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

describe("click sub-agent in rail (real SubagentsPanel)", () => {
  it("keeps the right-rail tab on Subagents (does NOT shift to Files)", async () => {
    // Setup: parent has one child sub-agent. User starts on parent
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
          children: [
            {
              id: "conv_child",
              title: null,
              tool: "researcher",
              session_name: null,
              current_task_status: null,
              busy: false,
              last_message_preview: null,
              pending_elicitations_count: 0,
            },
          ],
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
      // Simulate the real-world race: when the user navigates to a
      // child, ``useSession(conv_child)`` is loading on first render
      // — so ``activeSession`` is null. AppShell's
      // ``rootSessionId = activeSession?.parentSessionId ?? conversationId``
      // therefore briefly resolves to ``conv_child`` itself.
      if (id === "conv_child") {
        return { session: null, isLoading: true, error: null } as never;
      }
      return { session: null, isLoading: false, error: null };
    });

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Seed the child-sessions cache the way the rendered rail does in
    // production (its useChildSessions queries write here). The sticky
    // root holds across the parent→child click only while the target is
    // a known member of the last root's cached tree.
    qc.setQueryData(childSessionsQueryKey("conv_root"), [
      {
        id: "conv_child",
        title: null,
        tool: "researcher",
        session_name: null,
        current_task_status: null,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
    ]);
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_root?file=foo.txt"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="c/:conversationId" element={<div data-testid="page" />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
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
    // Exactly one child row should render for this fixture's one child;
    // zero means the panel never loaded, and more than one means duplicate
    // or unrelated rows could hide the navigation target.
    expect(childRows).toHaveLength(1);
    expect(childRows[0]).toHaveAttribute("href", "/c/conv_child");
    fireEvent.click(childRows[0]);

    // After navigation: the same rail Agents tab we clicked must STILL be
    // mounted and selected, not just any matching tab elsewhere in the shell.
    expect(agentsTab.isConnected).toBe(true);
    expect(agentsTab).toHaveAttribute("aria-selected", "true");
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
        <TooltipProvider>
          <MemoryRouter initialEntries={["/c/conv_solo"]}>
            <Routes>
              <Route element={<AppShell />}>
                <Route path="c/:conversationId" element={<div data-testid="page" />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );

    // The tab is present with the main-agent-only count; Files stays
    // the default selection.
    const agentsTab = screen.getByRole("tab", { name: /Agents\s*1/i });
    expect(agentsTab).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: /Files/i })).toHaveAttribute("aria-selected", "true");
  });
});
