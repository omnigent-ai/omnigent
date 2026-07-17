// Tests for Settings → Session management: active-session filters, loaded-row
// selection (including shift-range and ownership gating), and bulk
// archive/delete with confirmation + partial-failure recovery.

import { type ReactNode } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";

const mocks = vi.hoisted(() => ({
  conversations: [] as Conversation[],
  pages: undefined as Conversation[][] | undefined,
  projectNames: [] as string[],
  hasNextPage: false,
  isLoading: false,
  isError: false,
  isFetchingNextPage: false,
  fetchNextPage: vi.fn(),
  lastSearch: "" as string,
  lastIncludeArchived: undefined as boolean | undefined,
  lastProject: undefined as string | undefined,
  bulkArchiveMutate: vi.fn(),
  bulkDeleteMutate: vi.fn(),
  bulkArchivePending: false,
  bulkDeletePending: false,
}));

vi.mock("@/hooks/useConversations", async () => {
  const { useState } = await import("react");
  return {
    PROJECT_LABEL_KEY: "omni_project",
    useConversations: (
      searchQuery?: string,
      includeArchived?: boolean,
      _options?: unknown,
      project?: string,
    ) => {
      mocks.lastSearch = searchQuery ?? "";
      mocks.lastIncludeArchived = includeArchived;
      mocks.lastProject = project;
      const source = mocks.pages ?? [mocks.conversations];
      const [shown, setShown] = useState(1);
      const pages = source.slice(0, shown).map((rows) => ({
        data: project ? rows.filter((c) => c.labels?.["omni_project"] === project) : rows,
      }));
      return {
        data: { pages },
        isLoading: mocks.isLoading,
        isError: mocks.isError,
        hasNextPage: shown < source.length || mocks.hasNextPage,
        isFetchingNextPage: mocks.isFetchingNextPage,
        fetchNextPage: () => {
          mocks.fetchNextPage();
          setShown((n) => Math.min(n + 1, source.length));
        },
      };
    },
    useProjects: () => ({
      data: mocks.projectNames,
      isSuccess: true,
      isFetching: false,
    }),
    useBulkArchiveConversations: () => ({
      mutate: mocks.bulkArchiveMutate,
      isPending: mocks.bulkArchivePending,
    }),
    useBulkDeleteConversations: () => ({
      mutate: mocks.bulkDeleteMutate,
      isPending: mocks.bulkDeletePending,
    }),
  };
});

// Radix Select → native <select> for jsdom.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (v: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((c) => isValidElement(c) && c.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && trigger.props && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
      >
        {kids.filter((c) => !(isValidElement(c) && c.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => <>{children}</>,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});

import { SessionManagementSection } from "./SessionManagementSection";

function conv(id: string, partial: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...partial,
  };
}

function renderSection() {
  return render(
    <TooltipProvider>
      <MemoryRouter>
        <SessionManagementSection />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

function row(id: string) {
  return screen.getByTestId("session-mgmt-list").querySelector(`[data-session-id="${id}"]`)!;
}

beforeEach(() => {
  mocks.conversations = [];
  mocks.pages = undefined;
  mocks.projectNames = [];
  mocks.hasNextPage = false;
  mocks.isLoading = false;
  mocks.isError = false;
  mocks.isFetchingNextPage = false;
  mocks.fetchNextPage.mockReset();
  mocks.lastSearch = "";
  mocks.lastIncludeArchived = undefined;
  mocks.lastProject = undefined;
  mocks.bulkArchiveMutate.mockReset();
  mocks.bulkDeleteMutate.mockReset();
  mocks.bulkArchivePending = false;
  mocks.bulkDeletePending = false;
});
afterEach(cleanup);

describe("SessionManagementSection", () => {
  it("lists active sessions only and fetches without include_archived", () => {
    mocks.conversations = [
      conv("a", { title: "Active", archived: false }),
      conv("b", { title: "Also active" }),
      // Defensive filter: archived rows must not appear even if the server
      // somehow returned them on an active-only query.
      conv("c", { title: "Archived leak", archived: true }),
    ];
    renderSection();
    expect(mocks.lastIncludeArchived).toBe(false);
    const rows = screen.getAllByTestId("session-mgmt-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.queryByText("Archived leak")).toBeNull();
  });

  it("debounces search into the conversations query", async () => {
    mocks.conversations = [conv("a", { title: "Alpha" })];
    renderSection();
    fireEvent.change(screen.getByTestId("session-mgmt-search"), {
      target: { value: "alp" },
    });
    expect(mocks.lastSearch).toBe("");
    await waitFor(() => expect(mocks.lastSearch).toBe("alp"), { timeout: 1000 });
  });

  it("filters by project via the project picker", () => {
    mocks.projectNames = ["Alpha", "Beta"];
    mocks.conversations = [
      conv("a", { title: "Alpha chat", labels: { omni_project: "Alpha" } }),
      conv("b", { title: "Beta chat", labels: { omni_project: "Beta" } }),
    ];
    renderSection();
    expect(screen.getAllByTestId("session-mgmt-row")).toHaveLength(2);
    fireEvent.change(screen.getByTestId("session-mgmt-project-filter"), {
      target: { value: "project:Alpha" },
    });
    expect(mocks.lastProject).toBe("Alpha");
    const rows = screen.getAllByTestId("session-mgmt-row");
    expect(rows).toHaveLength(1);
    expect(within(rows[0]).getByText("Alpha chat")).toBeInTheDocument();
  });

  it("paginates with Load more", () => {
    mocks.pages = [[conv("p1", { title: "Page one" })], [conv("p2", { title: "Page two" })]];
    renderSection();
    expect(screen.getAllByTestId("session-mgmt-row")).toHaveLength(1);
    fireEvent.click(screen.getByTestId("session-mgmt-load-more"));
    expect(mocks.fetchNextPage).toHaveBeenCalled();
    expect(screen.getAllByTestId("session-mgmt-row")).toHaveLength(2);
  });

  it("shows shared sessions but disables selecting them", () => {
    mocks.conversations = [
      conv("owned", { title: "Mine", permission_level: 4 }),
      conv("shared", { title: "Theirs", permission_level: 2 }),
    ];
    renderSection();
    const shared = row("shared");
    expect(shared).toHaveAttribute("data-owned", "false");
    expect(shared).toHaveAttribute(
      "title",
      "You don’t own this session, so it can’t be archived or deleted here.",
    );
    fireEvent.click(shared);
    expect(screen.getByText("None selected")).toBeInTheDocument();
    fireEvent.click(row("owned"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("select all only covers owned loaded rows", () => {
    mocks.conversations = [
      conv("a", { title: "A", permission_level: 4 }),
      conv("b", { title: "B", permission_level: 4 }),
      conv("shared", { title: "Shared", permission_level: 1 }),
    ];
    renderSection();
    fireEvent.click(screen.getByTestId("session-mgmt-select-all"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(row("a")).toHaveAttribute("aria-checked", "true");
    expect(row("b")).toHaveAttribute("aria-checked", "true");
    expect(row("shared")).not.toHaveAttribute("aria-checked", "true");
  });

  it("supports shift-click range selection over owned rows", () => {
    mocks.conversations = [
      conv("a", { title: "A", permission_level: 4 }),
      conv("b", { title: "B", permission_level: 4 }),
      conv("c", { title: "C", permission_level: 4 }),
    ];
    renderSection();
    fireEvent.click(row("a"));
    fireEvent.click(row("c"), { shiftKey: true });
    expect(screen.getByText("3 selected")).toBeInTheDocument();
  });

  it("archives the current selection", () => {
    mocks.conversations = [
      conv("a", { title: "A", permission_level: 4 }),
      conv("b", { title: "B", permission_level: 4 }),
    ];
    renderSection();
    fireEvent.click(row("a"));
    fireEvent.click(row("b"));
    fireEvent.click(screen.getByTestId("session-mgmt-archive"));
    expect(mocks.bulkArchiveMutate).toHaveBeenCalledWith(
      { ids: ["a", "b"], archived: true },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });

  it("requires confirmation before deleting and warns about branches", () => {
    mocks.conversations = [conv("a", { title: "A", permission_level: 4 })];
    renderSection();
    fireEvent.click(row("a"));
    fireEvent.click(screen.getByTestId("session-mgmt-delete"));
    expect(mocks.bulkDeleteMutate).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: /Delete 1 session/ })).toBeInTheDocument();
    expect(screen.getByText(/Branches are not cleaned up/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("session-mgmt-confirm-delete"));
    expect(mocks.bulkDeleteMutate).toHaveBeenCalledWith(
      ["a"],
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });

  it("disables actions while a bulk mutation is pending", () => {
    mocks.conversations = [conv("a", { title: "A", permission_level: 4 })];
    mocks.bulkArchivePending = true;
    renderSection();
    fireEvent.click(row("a"));
    // Click is ignored while busy (disabled attribute on the button).
    expect(screen.getByTestId("session-mgmt-archive")).toBeDisabled();
    expect(screen.getByTestId("session-mgmt-delete")).toBeDisabled();
    expect(screen.getByTestId("session-mgmt-select-all")).toBeDisabled();
  });

  it("keeps failed IDs selected and offers retry/dismiss after partial archive failure", () => {
    mocks.conversations = [
      conv("a", { title: "A", permission_level: 4 }),
      conv("b", { title: "B", permission_level: 4 }),
    ];
    mocks.bulkArchiveMutate.mockImplementation(
      (_vars: unknown, opts?: { onError?: (err: { failed: string[]; total: number }) => void }) => {
        opts?.onError?.({ failed: ["b"], total: 2 });
      },
    );
    renderSection();
    fireEvent.click(screen.getByTestId("session-mgmt-select-all"));
    fireEvent.click(screen.getByTestId("session-mgmt-archive"));
    expect(screen.getByTestId("session-mgmt-bulk-error")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 archive actions failed/)).toBeInTheDocument();
    // Successful ID dropped; failed ID retained for retry.
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(row("b")).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByTestId("session-mgmt-dismiss-error"));
    expect(screen.queryByTestId("session-mgmt-bulk-error")).toBeNull();

    // Re-trigger a partial failure so Retry can be exercised.
    fireEvent.click(screen.getByTestId("session-mgmt-select-all"));
    fireEvent.click(screen.getByTestId("session-mgmt-archive"));
    mocks.bulkArchiveMutate.mockClear();
    // Next mutate succeeds so Retry clears the banner.
    mocks.bulkArchiveMutate.mockImplementation(
      (_vars: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );
    fireEvent.click(screen.getByTestId("session-mgmt-retry"));
    expect(mocks.bulkArchiveMutate).toHaveBeenCalledWith(
      { ids: ["b"], archived: true },
      expect.any(Object),
    );
    expect(screen.queryByTestId("session-mgmt-bulk-error")).toBeNull();
    expect(screen.getByText("None selected")).toBeInTheDocument();
  });

  it("shows the empty state when there are no active sessions", () => {
    renderSection();
    expect(screen.getByText("No active sessions.")).toBeInTheDocument();
  });
});
