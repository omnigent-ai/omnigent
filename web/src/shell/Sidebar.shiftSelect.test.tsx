// Tests for shift-click range selection in the sidebar's multi-session mode.
// Covers the pure range computation helper and the integrated click behavior.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

// ── Pure unit tests ─────────────────────────────────────────────────────────
import { computeShiftSelectRange } from "./Sidebar";

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

const { projectsMock, conversationsRef, projectSessionsMock } = vi.hoisted(() => ({
  projectsMock: [] as string[],
  conversationsRef: { current: [] as { id: string; labels?: Record<string, string> }[] },
  projectSessionsMock: { current: {} as Record<string, unknown[]> },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: projectsMock }),
  useProjectSessions: (project: string, enabled: boolean) => {
    const override = projectSessionsMock.current[project];
    const rows = !enabled
      ? []
      : (override ??
        conversationsRef.current.filter(
          (c) => (c.labels?.omni_project ?? null) === project && (c as any).archived !== true,
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
  fetchProjectSessionIds: vi.fn(() => Promise.resolve([] as string[])),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

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
  localStorage.clear();
  projectsMock.length = 0;
  projectSessionsMock.current = {};
});
afterEach(cleanup);

describe("Sidebar shift-click selection", () => {
  it("shift-click selects range between anchor and target within Chats", async () => {
    const sessions = [conv("s1"), conv("s2"), conv("s3"), conv("s4")];
    mockConversations(sessions);
    renderSidebar();

    // Enter selection mode
    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click first session (sets anchor)
    const row1 = screen.getByTitle("s1").closest("a")!;
    fireEvent.click(row1);

    // Shift-click third session
    const row3 = screen.getByTitle("s3").closest("a")!;
    fireEvent.click(row3, { shiftKey: true });

    // s1, s2, s3 should all be selected (bg-primary/5 class)
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("shift-click does not select project sessions when selecting within Chats", async () => {
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
    const rowC1 = screen.getByTitle("c1").closest("a")!;
    fireEvent.click(rowC1);

    // Shift-click c3 (last chat session)
    const rowC3 = screen.getByTitle("c3").closest("a")!;
    fireEvent.click(rowC3, { shiftKey: true });

    // Only c1, c2, c3 should be selected — NOT p1, p2
    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("normal click after shift-select sets a new anchor", async () => {
    const sessions = [conv("s1"), conv("s2"), conv("s3"), conv("s4")];
    mockConversations(sessions);
    renderSidebar();

    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click s1 (anchor), shift-click s2 (range: s1, s2)
    fireEvent.click(screen.getByTitle("s1").closest("a")!);
    fireEvent.click(screen.getByTitle("s2").closest("a")!, { shiftKey: true });

    await waitFor(() => {
      expect(screen.getByText("2 selected")).toBeInTheDocument();
    });

    // Normal click on s4 (sets new anchor, toggles s4 on)
    fireEvent.click(screen.getByTitle("s4").closest("a")!);

    await waitFor(() => {
      expect(screen.getByText("3 selected")).toBeInTheDocument();
    });
  });

  it("shift-select within a project uses the folder's own rendered IDs, not the global list", async () => {
    // Seed a project with sessions that differ from the global list:
    // the global list has p1,p2 but the folder's own query returns p1,p2,p3.
    projectsMock.push("Alpha");
    const sessions = [
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
      conv("c1"),
    ];
    mockConversations(sessions);
    // The folder's useProjectSessions returns an extra session (p3)
    // that isn't in the global paginated window.
    projectSessionsMock.current["Alpha"] = [
      conv("p1", { labels: { omni_project: "Alpha" } }),
      conv("p2", { labels: { omni_project: "Alpha" } }),
      conv("p3", { labels: { omni_project: "Alpha" } }),
    ];
    localStorage.setItem("omnigent:expanded-project-sections", JSON.stringify(["Alpha"]));

    renderSidebar();

    const selectBtn = screen.getByRole("button", { name: /select/i });
    fireEvent.click(selectBtn);

    // Click p1 (anchor) then shift-click p3 — the range should include
    // p1, p2, p3 (all from the folder's own query, including p3 which
    // isn't in the global list).
    fireEvent.click(screen.getByTitle("p1").closest("a")!);
    fireEvent.click(screen.getByTitle("p3").closest("a")!, { shiftKey: true });

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
    fireEvent.click(screen.getByTitle("s1").closest("a")!);
    fireEvent.click(screen.getByRole("button", { name: /exit/i }));

    // Re-enter, shift-click s3 — should single-toggle (no anchor)
    fireEvent.click(screen.getByRole("button", { name: /select/i }));
    fireEvent.click(screen.getByTitle("s3").closest("a")!, { shiftKey: true });

    await waitFor(() => {
      expect(screen.getByText("1 selected")).toBeInTheDocument();
    });
  });
});
