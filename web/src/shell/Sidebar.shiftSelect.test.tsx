// Tests for shift-click range selection in the sidebar's multi-session mode.
// Covers the pure range computation helper and the integrated click behavior.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type * as IdentityModule from "@/lib/identity";

// ── Pure unit tests ─────────────────────────────────────────────────────────
import { computeShiftSelectRange, Sidebar } from "./Sidebar";

describe("computeShiftSelectRange", () => {
  const ids = ["a", "b", "c", "d", "e"];

  it("selects forward range (anchor before target)", () => {
    expect(computeShiftSelectRange(ids, "b", "d")).toEqual(["b", "c", "d"]);
  });

  it("selects backward range (anchor after target)", () => {
    expect(computeShiftSelectRange(ids, "d", "b")).toEqual(["b", "c", "d"]);
  });

  it("selects single item when anchor equals target", () => {
    expect(computeShiftSelectRange(ids, "c", "c")).toEqual(["c"]);
  });

  it("selects entire list from first to last", () => {
    expect(computeShiftSelectRange(ids, "a", "e")).toEqual(["a", "b", "c", "d", "e"]);
  });

  it("returns null when anchor is not in the list", () => {
    expect(computeShiftSelectRange(ids, "z", "c")).toBeNull();
  });

  it("returns null when target is not in the list", () => {
    expect(computeShiftSelectRange(ids, "b", "z")).toBeNull();
  });

  it("returns null for an empty list", () => {
    expect(computeShiftSelectRange([], "a", "b")).toBeNull();
  });
});

// ── Integration tests ───────────────────────────────────────────────────────

const { projectsMock, conversationsRef, projectSessionsMock, bulkArchiveMock } = vi.hoisted(() => ({
  projectsMock: [] as string[],
  conversationsRef: {
    current: [] as { id: string; labels?: Record<string, string>; archived?: boolean }[],
  },
  projectSessionsMock: { current: {} as Record<string, unknown[]> },
  bulkArchiveMock: { mutate: vi.fn() },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({
    mutate: bulkArchiveMock.mutate,
    isPending: false,
    isError: false,
  }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkMoveToProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversations: () => ({
    data: { conversations: [], filterHonored: true },
    isSuccess: true,
  }),
  useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
  setConversationPinned: vi.fn(() => Promise.resolve({})),
  PINNED_CONVERSATIONS_KEY: ["pinned-conversations"],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: projectsMock.map((name: string) => ({ id: `p_${name}`, name })) }),
  useProjectSessions: (project: string, enabled: boolean) => {
    const override = projectSessionsMock.current[project];
    const rows = !enabled
      ? []
      : (override ??
        conversationsRef.current.filter(
          (c) => (c.labels?.omni_project ?? null) === project && c.archived !== true,
        ));
    return {
      data: enabled
        ? {
            pages: [{ data: rows, first_id: null, last_id: null, has_more: false }],
            pageParams: [undefined],
          }
        : undefined,
      isLoading: false,
      isError: false,
      error: null,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    };
  },
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useRenameProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useCreateProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useProjectConfig: () => ({ data: undefined, isLoading: false }),
  useUpdateProjectConfig: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: vi.fn(() => Promise.resolve([] as string[])),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

// Resolve the viewer to a fixed id so a conversation whose `owner` differs
// reads as "not owned" (drives the mixed-ownership Delete-count assertion).
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "viewer",
  resolveIdentity: () => Promise.resolve("viewer"),
}));

import { useConversations } from "@/hooks/useConversations";

const useConvMock = vi.mocked(useConversations);

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    agent_name: "Claude Code",
    ...partial,
  };
}

function mockConversations(convs: Conversation[]) {
  conversationsRef.current = convs;
  useConvMock.mockImplementation(
    () =>
      ({
        data: {
          pages: [
            {
              data: convs,
              first_id: convs[0]?.id ?? null,
              last_id: convs.at(-1)?.id ?? null,
              has_more: false,
            },
          ],
          pageParams: [undefined],
        },
        isLoading: false,
        isError: false,
        error: null,
        fetchNextPage: vi.fn(),
        hasNextPage: false,
        isFetchingNextPage: false,
      }) as unknown as ReturnType<typeof useConversations>,
  );
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  bulkArchiveMock.mutate.mockReset();
  localStorage.clear();
  projectsMock.length = 0;
  projectSessionsMock.current = {};
});
afterEach(cleanup);

describe("Sidebar shift-click selection", () => {
  it("shift-click selects range between anchor and target within Sessions", async () => {
    const sessions = [conv("s1"), conv("s2"), conv("s3"), conv("s4")];
    mockConversations(sessions);
    renderSidebar();

    // Enter selection mode
    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click first session (sets anchor)
    const row1 = screen.getByRole("link", { name: "s1" });
    fireEvent.click(row1);

    // Shift-click third session
    const row3 = screen.getByRole("link", { name: "s3" });
    fireEvent.click(row3, { shiftKey: true });

    // s1, s2, s3 should all be selected (bg-primary/5 class)
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("shift-click does not select project sessions when selecting within Sessions", async () => {
    // Set up project "Alpha" with 2 sessions, plus 3 unfiled chat sessions
    projectsMock.push("Alpha");
    const sessions = [
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
      conv("c1"),
      conv("c2"),
      conv("c3"),
    ];
    mockConversations(sessions);

    // Expand the Alpha project so its sessions are visible
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));

    renderSidebar();

    // Enter selection mode
    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click c1 (first chat session, sets anchor)
    const rowC1 = screen.getByRole("link", { name: "c1" });
    fireEvent.click(rowC1);

    // Shift-click c3 (last chat session)
    const rowC3 = screen.getByRole("link", { name: "c3" });
    fireEvent.click(rowC3, { shiftKey: true });

    // Only c1, c2, c3 should be selected — NOT p1, p2
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("selects sessions within projects (not the flat list) from the Projects kebab", async () => {
    // Project "Alpha" has 3 sessions; the flat list has 2 unfiled chats.
    projectsMock.push("Alpha");
    const sessions = [
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
      conv("p3", { labels: { omni_project: "Alpha" } }),
      conv("c1"),
      conv("c2"),
    ];
    mockConversations(sessions);
    // Selection mode preserves the current expansion; seed Alpha expanded so
    // its sessions are visible (the kebab no longer auto-expands folders).
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));
    renderSidebar();

    // Enter selection mode via the Projects header kebab → project scope.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Project list actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("projects-select-sessions"));

    // Shift-select across the project sessions: p1 anchor → p3 selects p1..p3.
    fireEvent.click(await screen.findByRole("link", { name: "p1" }));
    fireEvent.click(screen.getByRole("link", { name: "p3" }), { shiftKey: true });
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });

    // A flat-list chat is NOT selectable in project scope: clicking it does not
    // grow the selection (it has no checkbox and its link stays a navigation).
    fireEvent.click(screen.getByRole("link", { name: "c1" }), { shiftKey: true });
    expect(screen.getByText("3 selected")).toBeInTheDocument();
  });

  it("resolves a folder session outside the global window for shift-select AND bulk archive", async () => {
    // Regression: the folder paginates independently of the global list, so it
    // can render a member (p3) the global window hasn't loaded. Projects-scope
    // selection must resolve rows against the folder's own query, not the global
    // list — otherwise p3 would toggle a count but silently drop from the range
    // and from the bulk action.
    projectsMock.push("Alpha");
    // Global window: only p1, p2 (p3 is beyond it).
    mockConversations([
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
    ]);
    // The folder's OWN query returns p1, p2, p3 — p3 is out-of-window.
    projectSessionsMock.current["Alpha"] = [
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
      conv("p3", { labels: { omni_project: "Alpha" } }),
    ];
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));
    renderSidebar();

    fireEvent.pointerDown(screen.getByRole("button", { name: "Project list actions" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByTestId("projects-select-sessions"));

    // Shift-select p1 → p3: the range must span all three (including the
    // out-of-window p3), not collapse to a single toggle.
    fireEvent.click(await screen.findByRole("link", { name: "p1" }));
    fireEvent.click(screen.getByRole("link", { name: "p3" }), { shiftKey: true });
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });

    // Bulk-archive must fire with ALL three ids — p3 must not be silently
    // dropped because the global window didn't resolve it.
    fireEvent.click(screen.getByTestId("bulk-archive"));
    await waitFor(() => {
      expect(bulkArchiveMock.mutate).toHaveBeenCalledTimes(1);
    });
    const [{ ids, archived }] = bulkArchiveMock.mutate.mock.calls[0];
    expect(archived).toBe(true);
    expect([...ids].sort()).toEqual(["p1", "p2", "p3"]);
  });

  it("labels Delete with the owned count when the selection is mixed-ownership", async () => {
    // The flat "All sessions" list mixes the viewer's own sessions with ones
    // shared to them by other owners. Delete acts only on owned rows, so its
    // label must reflect the owned count — not the raw "N selected" — when they
    // differ. (Project folders are owner-only, so mixed ownership only arises in
    // the flat list, not a folder.)
    const mine = conv("mine", { owner: "viewer" });
    const theirs = conv("theirs", { owner: "someone_else" });
    mockConversations([mine, theirs]);
    renderSidebar();

    fireEvent.click(screen.getByRole("button", { name: /select/i }));

    // Select both rows — "2 selected", but only the owned one is deletable.
    fireEvent.click(await screen.findByRole("link", { name: "mine" }));
    fireEvent.click(screen.getByRole("link", { name: "theirs" }));
    await waitFor(() => {
      expect(screen.getByText("2 selected")).toBeInTheDocument();
    });

    // Delete label carries the owned count (1), diverging from the "2 selected".
    expect(screen.getByRole("button", { name: "Delete 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("normal click after shift-select sets a new anchor", async () => {
    const sessions = [conv("s1"), conv("s2"), conv("s3"), conv("s4")];
    mockConversations(sessions);
    renderSidebar();

    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click s1 (anchor), shift-click s2 (range: s1, s2)
    fireEvent.click(screen.getByRole("link", { name: "s1" }));
    fireEvent.click(screen.getByRole("link", { name: "s2" }), { shiftKey: true });

    await waitFor(() => {
      expect(screen.getByText("2 selected")).toBeInTheDocument();
    });

    // Normal click on s4 (sets new anchor, toggles s4 on)
    fireEvent.click(screen.getByRole("link", { name: "s4" }));

    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("anchor resets when exiting and re-entering selection mode", async () => {
    const sessions = [conv("s1"), conv("s2"), conv("s3")];
    mockConversations(sessions);
    renderSidebar();

    // Enter, click s1, exit
    fireEvent.click(screen.getByRole("button", { name: /select/i }));
    fireEvent.click(screen.getByRole("link", { name: "s1" }));
    fireEvent.click(screen.getByRole("button", { name: /exit/i }));

    // Re-enter, shift-click s3 — should single-toggle (no anchor)
    fireEvent.click(screen.getByRole("button", { name: /select/i }));
    fireEvent.click(screen.getByRole("link", { name: "s3" }), { shiftKey: true });

    await waitFor(() => {
      expect(screen.getByText("1 selected")).toBeInTheDocument();
    });
  });
});
