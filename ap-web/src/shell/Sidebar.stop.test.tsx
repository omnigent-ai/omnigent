// Tests for the sidebar kebab's "Stop session" item (moved here from the
// chat header). Contract: the item renders only for stoppable sessions
// (isSessionStoppable: host-spawned or claude-native) whose runner isn't
// known-offline, is owner-gated (disabled + tooltip for non-owners), and
// confirms through a dialog before firing the stop mutation. See
// ConversationRow in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable stop mutation + runner-liveness lookup, declared via
// vi.hoisted so the vi.mock factories can reference them. The dialog reads
// isPending/isError on every render and reset() on open, so the stub
// carries the full mutation shape (not just mutate).
const mocks = vi.hoisted(() => ({
  stop: { mutate: vi.fn(), reset: vi.fn(), isPending: false, isError: false },
  runnerOnline: vi.fn<(id: string | undefined) => boolean | undefined>(() => undefined),
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useSetSessionLabel: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => mocks.stop,
}));

vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/RunnerHealthProvider")>()),
  useSessionRunnerOnline: (id: string | undefined) => mocks.runnerOnline(id),
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// Owner (permission_level null) of a host-spawned session → stoppable.
const HOST_SPAWNED: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null,
  host_id: "host_a1b2",
  runner_id: "runner_token_abc",
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
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
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** Open the row's action dropdown (Radix opens on pointerdown, not click). */
function openKebab() {
  fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
}

beforeEach(() => {
  mocks.stop.mutate.mockReset();
  mocks.stop.reset.mockReset();
  mocks.runnerOnline.mockReset();
  mocks.runnerOnline.mockReturnValue(undefined);
});

afterEach(() => {
  cleanup();
});

describe("sidebar Stop session item", () => {
  it("stops a host-spawned session after dialog confirm", () => {
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    fireEvent.click(screen.getByTestId("stop-conversation"));

    // The confirm dialog gates the mutation — nothing fires on item click.
    expect(mocks.stop.mutate).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Stop session" }));
    expect(mocks.stop.mutate).toHaveBeenCalledTimes(1);
    // Failure: the dialog stopped a different row's session.
    expect(mocks.stop.mutate.mock.calls[0][0]).toBe("conv_1");
  });

  it("clears a prior stop failure when the dialog is opened", () => {
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    fireEvent.click(screen.getByTestId("stop-conversation"));

    // Failure: reset() not invoked on the menu item's onSelect — a stale
    // "couldn't stop" error from a previous attempt would greet the
    // reopened dialog. The reset can't live on the Dialog's onOpenChange:
    // Radix doesn't fire it for this programmatic (setState) open.
    expect(mocks.stop.reset).toHaveBeenCalledTimes(1);
  });

  it("shows for a CLI-launched claude-native session (no host)", () => {
    // Regression guard: the wrapper label keeps the item without a host.
    mockConversations([
      {
        ...HOST_SPAWNED,
        host_id: undefined,
        runner_id: undefined,
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      },
    ]);
    renderSidebar();
    openKebab();
    expect(screen.getByTestId("stop-conversation")).toBeInTheDocument();
  });

  it("is hidden for a local in-process runner (runner_id, no host_id)", () => {
    // runner_id but no host_id → no kill path → hidden.
    mockConversations([{ ...HOST_SPAWNED, host_id: undefined }]);
    renderSidebar();
    openKebab();
    expect(screen.queryByTestId("stop-conversation")).toBeNull();
  });

  it("is hidden when the runner is known offline", () => {
    // The session is already stopped — no destructive control to offer.
    mocks.runnerOnline.mockReturnValue(false);
    mockConversations([HOST_SPAWNED]);
    renderSidebar();
    openKebab();
    expect(screen.queryByTestId("stop-conversation")).toBeNull();
  });

  it("is disabled for non-owners even on a stoppable session", () => {
    // Owner-gated server-side; a shared viewer (level 1) sees it disabled.
    mockConversations([{ ...HOST_SPAWNED, permission_level: 1 }]);
    renderSidebar();
    openKebab();
    const item = screen.getByTestId("stop-conversation");
    expect(item).toHaveAttribute("data-disabled");
  });
});
