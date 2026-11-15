import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BoardPage } from "./BoardPage";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { Conversation } from "@/hooks/useConversations";
import * as conversationsHook from "@/hooks/useConversations";
import * as automationInboxHook from "@/hooks/useAutomationInbox";
import * as commentInboxHook from "@/hooks/useCommentInbox";
import { SidebarDataProvider } from "@/hooks/useSidebarData";
import { resetReadStateForTests, seedReadState } from "@/hooks/useUnseenConversations";
import type * as identity from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import { BOARD_ACCEPTED_LABEL_KEY } from "@/lib/sessionBoard";

vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useConversations: vi.fn(),
}));
vi.mock("@/hooks/useAutomationInbox", () => ({ useAutomationInbox: vi.fn() }));
vi.mock("@/hooks/useCommentInbox", () => ({ useCommentInbox: vi.fn() }));
vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_review",
    object: "conversation",
    title: "Review me",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: {},
    permission_level: null,
    status: "idle",
    pending_elicitations_count: 0,
    archived: false,
    ...overrides,
  };
}

function conversationsStub(rows: Conversation[], overrides: Record<string, unknown> = {}) {
  return {
    data: { pages: [{ data: rows }] },
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    ...overrides,
  } as unknown as ReturnType<typeof conversationsHook.useConversations>;
}

function mockRows(rows: Conversation[], overrides: Record<string, unknown> = {}) {
  vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub(rows, overrides));
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, features: { session_board: true } }}>
        <SidebarDataProvider>
          <MemoryRouter>
            <TooltipProvider>
              <BoardPage />
            </TooltipProvider>
          </MemoryRouter>
        </SidebarDataProvider>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

function column(id: string) {
  return within(screen.getByTestId(`board-column-${id}`));
}

function patchBodies() {
  return vi
    .mocked(authenticatedFetch)
    .mock.calls.filter(([, init]) => init?.method === "PATCH")
    .map(([url, init]) => ({ url, body: JSON.parse(String(init?.body)) }));
}

beforeEach(() => {
  vi.mocked(automationInboxHook.useAutomationInbox).mockReturnValue({
    items: [],
    isLoading: false,
    isError: false,
    hasNextPage: false,
    markRead: vi.fn(),
    refetch: vi.fn(),
    fetchNextPage: vi.fn(),
  } as unknown as ReturnType<typeof automationInboxHook.useAutomationInbox>);
  vi.mocked(commentInboxHook.useCommentInbox).mockReturnValue({
    items: [],
    isLoading: false,
    failedCount: 0,
    retryFailed: vi.fn(),
  });
  vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) =>
    init?.method === "PATCH"
      ? new Response(JSON.stringify(conversation()), { status: 200 })
      : new Response(JSON.stringify({ object: "list", data: [], has_more: false }), {
          status: 200,
        }),
  );
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetReadStateForTests();
  localStorage.clear();
});

describe("BoardPage", () => {
  it("places each of the viewer's sessions in its work-state column", () => {
    mockRows([
      conversation({ id: "conv_running", title: "Building", status: "running" }),
      conversation({
        id: "conv_waiting",
        title: "Asks first",
        status: "running",
        pending_elicitations_count: 2,
      }),
      conversation({ id: "conv_review", title: "Review me" }),
      conversation({
        id: "conv_done",
        title: "Shipped",
        labels: { [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" },
      }),
      conversation({ id: "conv_shared", title: "Someone else's", permission_level: 2 }),
    ]);
    renderPage();

    expect(column("running").getByRole("link", { name: "Building" })).toHaveAttribute(
      "href",
      "/c/conv_running",
    );
    expect(column("needs_you").getByText("Asks first")).toBeInTheDocument();
    expect(column("needs_you").getByText("2 approvals waiting")).toBeInTheDocument();
    expect(column("needs_review").getByText("Review me")).toBeInTheDocument();
    expect(column("done").getByText("Shipped")).toBeInTheDocument();
    expect(column("done").getByRole("button", { name: "Reopen Shipped" })).toBeInTheDocument();
    // Shared sessions stay off the viewer's board.
    expect(screen.queryByText("Someone else's")).toBeNull();
    expect(screen.getAllByTestId("board-card")).toHaveLength(4);
  });

  it("marks unread work in Needs review", () => {
    const row = conversation({ id: "conv_unread", title: "Fresh output" });
    seedReadState([{ id: row.id, viewer_last_seen: row.updated_at - 50 }]);
    mockRows([row]);
    renderPage();

    const card = column("needs_review").getByTestId("board-card");
    expect(within(card).getByText("New messages")).toBeInTheDocument();
    expect(within(card).getByRole("img", { name: "New messages" })).toBeInTheDocument();
  });

  it("accepts a reviewed session at its current activity time", async () => {
    mockRows([conversation()]);
    renderPage();

    fireEvent.click(column("needs_review").getByRole("button", { name: "Accept Review me" }));

    await waitFor(() =>
      expect(patchBodies()).toEqual([
        {
          url: "/v1/sessions/conv_review",
          body: { labels: { [BOARD_ACCEPTED_LABEL_KEY]: "1700000100" } },
        },
      ]),
    );
  });

  it("reopens an accepted session by clearing its acceptance", async () => {
    mockRows([
      conversation({ title: "Shipped", labels: { [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" } }),
    ]);
    renderPage();

    fireEvent.click(column("done").getByRole("button", { name: "Reopen Shipped" }));

    await waitFor(() =>
      expect(patchBodies()).toEqual([
        { url: "/v1/sessions/conv_review", body: { labels: { [BOARD_ACCEPTED_LABEL_KEY]: "" } } },
      ]),
    );
  });

  it("disables Accept while that session's write is in flight", async () => {
    let respond: (response: Response) => void = () => {};
    vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) =>
      init?.method === "PATCH"
        ? new Promise<Response>((resolve) => {
            respond = resolve;
          })
        : new Response(JSON.stringify({ object: "list", data: [], has_more: false })),
    );
    mockRows([conversation(), conversation({ id: "conv_other", title: "Other" })]);
    renderPage();

    const accept = column("needs_review").getByRole("button", { name: "Accept Review me" });
    fireEvent.click(accept);

    await waitFor(() => expect(accept).toBeDisabled());
    expect(column("needs_review").getByRole("button", { name: "Accept Other" })).toBeEnabled();
    respond(new Response(JSON.stringify(conversation()), { status: 200 }));
    await waitFor(() => expect(accept).toBeEnabled());
  });

  it("shows an accepted failed turn in Done without the failure icon", () => {
    mockRows([
      conversation({
        title: "Broke, accepted",
        status: "failed",
        labels: { [BOARD_ACCEPTED_LABEL_KEY]: "1700000101" },
      }),
    ]);
    renderPage();

    const card = column("done").getByTestId("board-card");
    expect(within(card).getByText("Accepted")).toBeInTheDocument();
    expect(within(card).queryByTestId("session-state-badge")).toBeNull();
  });

  it("offers Accept on a failed turn but not on running work", () => {
    mockRows([
      conversation({ id: "conv_failed", title: "Broke", status: "failed" }),
      conversation({ id: "conv_running", title: "Busy", status: "running" }),
    ]);
    renderPage();

    expect(column("needs_you").getByText("Last turn failed")).toBeInTheDocument();
    expect(column("needs_you").getByRole("button", { name: "Accept Broke" })).toBeInTheDocument();
    expect(within(column("running").getByTestId("board-card")).queryByRole("button")).toBeNull();
  });

  it("shows an empty state with no sessions and loads more on request", () => {
    const fetchNextPage = vi.fn();
    mockRows([], { hasNextPage: true, fetchNextPage });
    renderPage();

    expect(screen.getByText("No sessions yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more sessions" }));
    expect(fetchNextPage).toHaveBeenCalled();
  });
});
