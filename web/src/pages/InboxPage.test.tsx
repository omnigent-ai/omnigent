import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Tests for the Inbox page (`/inbox`) — the cross-session list of pending
// approval prompts and unseen file comments.
//
// The page composes several live data sources, so we mock at their seams:
//  - `useConversations` (the session list + its paging drain),
//  - `useCommentInbox` (the comment side of the inbox),
//  - `getSession` / `approve` (per-session snapshot fetch + the verdict POST).
// The pure assembly helper `collectInboxItems` and the display helpers are
// left REAL, so raw `response.elicitation_request` event dicts flow through
// the same parse path the app uses. `ApprovalCard` is stubbed to a minimal
// component that just exposes an Accept button wired to its `onSubmit`, which
// lets us drive the approve/rollback path without the real card's internals.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { InboxPage } from "./InboxPage";
import type { Conversation } from "@/hooks/useConversations";
import * as conversationsHook from "@/hooks/useConversations";
import * as commentInboxHook from "@/hooks/useCommentInbox";
import * as sessionsApi from "@/lib/sessionsApi";
import type { CommentInbox } from "@/hooks/useCommentInbox";
import {
  clearAskUserQuestionDrafts,
  getAskUserQuestionDraft,
  isApprovalInFlight,
  setAskUserQuestionDraft,
} from "@/lib/askUserQuestionDrafts";
import {
  isExplicitlyUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";

// Minimal ApprovalCard stub: renders the message and an Accept button that
// forwards to the page's submit handler. The real card's form/preview UX is
// out of scope here — we only need to exercise `makeSubmit` → `approve`.
vi.mock("@/components/blocks/ApprovalCard", () => ({
  ApprovalCard: ({
    elicitationId,
    message,
    status,
    allowAutoMode,
    onSubmit,
  }: {
    elicitationId: string;
    message: string;
    status: string;
    allowAutoMode?: boolean;
    onSubmit: (id: string, action: "accept" | "decline", content?: Record<string, unknown>) => void;
  }) => (
    <div data-testid="approval-card" data-status={status}>
      <span>{message}</span>
      <button type="button" onClick={() => onSubmit(elicitationId, "accept")}>
        Stub Accept
      </button>
      {allowAutoMode && (
        <button
          type="button"
          onClick={() => onSubmit(elicitationId, "accept", { allow_auto_mode: true })}
        >
          Stub Auto Mode
        </button>
      )}
    </div>
  ),
}));

vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useConversations: vi.fn(),
}));
vi.mock("@/hooks/useCommentInbox", () => ({ useCommentInbox: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({
  getSession: vi.fn(),
  approve: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
}));

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "sess_1",
    object: "conversation",
    title: "My Session",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    labels: {},
    permission_level: null,
    pending_elicitations_count: 1,
    archived: false,
    ...overrides,
  };
}

/** A raw `response.elicitation_request` event dict, as a snapshot replays it. */
function rawElicitation(id: string, message: string, extra: Record<string, unknown> = {}) {
  return {
    elicitation_id: id,
    params: { message, mode: "form", ...extra },
  };
}

/** Build a useConversations infinite-query stub for the given rows/paging. */
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

/** Build a CommentInbox stub; empty and settled by default. */
function commentInboxStub(overrides: Partial<CommentInbox> = {}): CommentInbox {
  return {
    items: [],
    isLoading: false,
    failedCount: 0,
    retryFailed: vi.fn(),
    ...overrides,
  };
}

function renderPage() {
  // Fresh client per render so cached queries never leak between tests.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SidebarDataProvider>
        <MemoryRouter>
          <InboxPage />
        </MemoryRouter>
      </SidebarDataProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([]));
  vi.mocked(commentInboxHook.useCommentInbox).mockReturnValue(commentInboxStub());
  vi.mocked(sessionsApi.getSession).mockResolvedValue({
    pendingElicitations: [],
  } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
  vi.mocked(sessionsApi.approve).mockResolvedValue(
    {} as Awaited<ReturnType<typeof sessionsApi.approve>>,
  );
  vi.mocked(sessionsApi.fetchSessionItemsPage).mockResolvedValue({ items: [], hasMore: false });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetReadStateForTests();
  localStorage.clear();
  clearAskUserQuestionDrafts();
});

/** A finished session whose latest turn the viewer hasn't opened yet. */
function unreadConversation(overrides: Partial<Conversation> = {}): Conversation {
  const row = conversation({
    id: "sess_unread",
    title: "Unread Session",
    status: "idle",
    pending_elicitations_count: 0,
    updated_at: 1_700_000_100,
    ...overrides,
  });
  // Last seen before the turn finished — the sidebar dot's condition.
  seedReadState([{ id: row.id, viewer_last_seen: row.updated_at - 100 }]);
  return row;
}

function assistantReply(text: string) {
  return {
    id: "item_reply",
    type: "message",
    role: "assistant",
    content: [{ type: "output_text", text }],
  } as unknown as Awaited<ReturnType<typeof sessionsApi.fetchSessionItemsPage>>["items"][number];
}

function selectTab(name: string) {
  // Radix tabs activate on focus; jsdom's click doesn't move focus.
  const tab = screen.getByRole("tab", { name });
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

describe("InboxPage states", () => {
  it("shows a loading state while the session list is still loading", () => {
    // WHY: an in-flight (assembling) list with no items yet must show the
    // loading row, never the empty state.
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([], { isLoading: true }),
    );
    renderPage();
    expect(screen.getByText("Loading inbox…")).toBeInTheDocument();
  });

  it("shows the empty state once settled with nothing waiting", async () => {
    // WHY: a settled list with no approvals and no comments shows the
    // "Nothing waiting on you" empty state.
    renderPage();
    expect(await screen.findByText("Nothing waiting on you")).toBeInTheDocument();
  });

  it("offers manual loading when older sessions remain", () => {
    // WHY: `hasNextPage` keeps the inbox in the assembling state — an empty
    // `items` then only means "not done paging", so no empty state.
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([], { hasNextPage: true }),
    );
    renderPage();
    expect(screen.getByRole("button", { name: "Load more sessions" })).toBeInTheDocument();
  });

  it("loads additional scope pages only after a click", async () => {
    // WHY: an awaiting session may sit below the first page, so the inbox
    // calls fetchNextPage whenever another page is available.
    const fetchNextPage = vi.fn();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([], { hasNextPage: true, fetchNextPage }),
    );
    renderPage();
    expect(fetchNextPage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Load more sessions" }));
    await waitFor(() => expect(fetchNextPage).toHaveBeenCalled());
  });
});

describe("InboxPage approval items", () => {
  it("renders an approval card for a session with a pending prompt", async () => {
    // WHY: a row with a pending count fetches its snapshot, whose parsed
    // elicitation becomes a card; the first card is expanded by default.
    const row = conversation({ id: "sess_1", title: "My Session" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this dangerous op?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    expect(await screen.findByText("Approve this dangerous op?")).toBeInTheDocument();
    const item = screen.getByTestId("inbox-item");
    expect(item).toHaveAttribute("data-expanded", "true");
    // Header reflects the count summary.
    expect(screen.getByText(/1 approval/)).toBeInTheDocument();
  });

  it("excludes archived rows and rows with no pending prompts", async () => {
    // WHY: `rows` filters to non-archived rows with pending_elicitations_count
    // > 0, so neither an archived row nor a zero-count row mounts a snapshot.
    const rows = [
      conversation({ id: "archived", pending_elicitations_count: 3, archived: true }),
      conversation({ id: "settled", pending_elicitations_count: 0 }),
    ];
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub(rows));
    renderPage();

    expect(await screen.findByText("Nothing waiting on you")).toBeInTheDocument();
    expect(sessionsApi.getSession).not.toHaveBeenCalled();
  });

  it("collapses an item when its toggle is clicked and hides the card", async () => {
    // WHY: clicking the row toggle flips the expanded override, collapsing the
    // (otherwise-default-expanded) first item so its ApprovalCard unmounts.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    const item = await screen.findByTestId("inbox-item");
    const toggle = within(item).getByRole("button", { name: /My Session/ });
    expect(toggle).toHaveClass("cursor-pointer");
    fireEvent.click(toggle);
    await waitFor(() => expect(item).toHaveAttribute("data-expanded", "false"));
    expect(screen.queryByTestId("approval-card")).not.toBeInTheDocument();
  });

  it("submits an approve verdict via approve() and flips the card to responded", async () => {
    // WHY: clicking Accept optimistically marks responded then POSTs the
    // verdict through `approve()` to the resolve-target session.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));

    await waitFor(() =>
      expect(sessionsApi.approve).toHaveBeenCalledWith("sess_1", "eli_1", { action: "accept" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "responded"),
    );
  });

  it("offers auto mode from a snapshot and sends the choice to the owning session", async () => {
    const row = conversation({ id: "parent" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [
        rawElicitation("eli_auto", "Claude needs permission", {
          target_session_id: "child",
          allow_auto_mode: true,
        }),
      ],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Auto Mode" }));
    await waitFor(() =>
      expect(sessionsApi.approve).toHaveBeenCalledWith("child", "eli_auto", {
        action: "accept",
        content: { allow_auto_mode: true },
      }),
    );
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "responded");
  });

  it("rolls back the optimistic verdict when approve() rejects", async () => {
    // WHY: a failed resolve POST deletes the responded entry so the card
    // returns to pending and the user can retry.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    vi.mocked(sessionsApi.approve).mockRejectedValue(new Error("nope"));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    // After the rejection settles, the card is back to pending.
    await waitFor(() =>
      expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "pending"),
    );
  });

  it("clears an AskUserQuestion draft once the Inbox resolve POST succeeds", async () => {
    // WHY: answering from the Inbox retires the saved draft only on a
    // confirmed resolve, matching the chat store's POST-success cleanup.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    setAskUserQuestionDraft("eli_1", {
      currentIndex: 0,
      selections: { Q: "A" },
      customSelected: {},
      customInputs: {},
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    await waitFor(() => expect(getAskUserQuestionDraft("eli_1")).toBeUndefined());
  });

  it("keeps an AskUserQuestion draft when the Inbox resolve POST rejects", async () => {
    // WHY: the rollback must leave the draft so the restored card can retry
    // with the same answers.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    vi.mocked(sessionsApi.approve).mockRejectedValue(new Error("nope"));
    setAskUserQuestionDraft("eli_1", {
      currentIndex: 0,
      selections: { Q: "A" },
      customSelected: {},
      customInputs: {},
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    await waitFor(() =>
      expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "pending"),
    );
    expect(getAskUserQuestionDraft("eli_1")).toBeDefined();
  });

  it("marks the elicitation in flight only until the Inbox resolve POST settles", async () => {
    // WHY: the shared in-flight guard must cover the Inbox optimistic flip too,
    // so a restored transcript card keeps its draft until this POST confirms.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    let releaseApprove: (() => void) | null = null;
    vi.mocked(sessionsApi.approve).mockReturnValue(
      new Promise<Awaited<ReturnType<typeof sessionsApi.approve>>>((resolve) => {
        releaseApprove = () => resolve({} as Awaited<ReturnType<typeof sessionsApi.approve>>);
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    // The optimistic flip registered the id; the POST is still open.
    await waitFor(() => expect(isApprovalInFlight("eli_1")).toBe(true));
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "responded");

    await act(async () => {
      releaseApprove?.();
    });
    await waitFor(() => expect(isApprovalInFlight("eli_1")).toBe(false));
  });

  it("clears the in-flight mark when the Inbox resolve POST rejects", async () => {
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    let rejectApprove: (() => void) | null = null;
    vi.mocked(sessionsApi.approve).mockReturnValue(
      new Promise<Awaited<ReturnType<typeof sessionsApi.approve>>>((_resolve, reject) => {
        rejectApprove = () => reject(new Error("nope"));
      }),
    );
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    await waitFor(() => expect(isApprovalInFlight("eli_1")).toBe(true));

    await act(async () => {
      rejectApprove?.();
    });
    // The finally-equivalent clear runs on the rejection path too.
    await waitFor(() => expect(isApprovalInFlight("eli_1")).toBe(false));
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "pending");
  });

  it("clears a stale verdict when a snapshot refresh still shows the elicitation as pending", async () => {
    // WHY: when a hook retry re-parks the same elicitation id, the local
    // `responded` entry from the first approval keeps the card stuck on
    // "Approved". After the snapshot query delivers fresh data that still
    // lists the id as pending, the stale verdict must be cleared so the
    // approve button reappears.
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <SidebarDataProvider>
          <MemoryRouter>
            <InboxPage />
          </MemoryRouter>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    // Approve the card — it flips to responded.
    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    await waitFor(() =>
      expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "responded"),
    );

    // Simulate a snapshot refresh that still lists the same elicitation
    // (hook retry re-parked it). Update the row's updated_at so the
    // query key changes, which triggers a refetch with fresh data.
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    const updatedRow = conversation({ id: "sess_1", updated_at: 1_700_000_001 });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([updatedRow]));
    rerender(
      <QueryClientProvider client={queryClient}>
        <SidebarDataProvider>
          <MemoryRouter>
            <InboxPage />
          </MemoryRouter>
        </SidebarDataProvider>
      </QueryClientProvider>,
    );

    // The stale verdict should be cleared — card reverts to pending.
    await waitFor(() =>
      expect(screen.getByTestId("approval-card")).toHaveAttribute("data-status", "pending"),
    );
  });

  it("routes the verdict to the child session when the prompt is mirrored", async () => {
    // WHY: a mirrored child prompt carries target_session_id; the resolve POST
    // must target that session, not the row it surfaced under.
    const row = conversation({ id: "parent" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [
        rawElicitation("eli_child", "Child approval?", { target_session_id: "child" }),
      ],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Stub Accept" }));
    await waitFor(() =>
      expect(sessionsApi.approve).toHaveBeenCalledWith("child", "eli_child", { action: "accept" }),
    );
  });
});

describe("InboxPage comments and errors", () => {
  it("renders unseen file comments with author, path, and body", async () => {
    // WHY: an unseen comment collapses to one row naming its author; expanding
    // it shows the file path, body, and the deep link that marks it seen.
    vi.mocked(commentInboxHook.useCommentInbox).mockReturnValue(
      commentInboxStub({
        items: [
          {
            row: conversation({ id: "sess_1", pending_elicitations_count: 0 }),
            comment: {
              id: "cm_1",
              path: "src/app.ts",
              body: "Please reconsider this line.",
              created_by: "alice",
              created_at: 1_700_000_000,
              updated_at: 1_700_000_000_000,
              status: "draft",
            } as CommentInbox["items"][number]["comment"],
          },
        ],
      }),
    );
    renderPage();

    const item = await screen.findByTestId("inbox-comment");
    expect(item).toHaveAttribute("data-expanded", "false");
    // Collapsed: the author's avatar tile plus "author: body" in the preview.
    expect(item).toHaveTextContent("alice: Please reconsider this line.");
    expect(within(item).getByRole("img", { name: "Unread" })).toBeInTheDocument();
    expect(screen.getByText("1 comment")).toBeInTheDocument();

    fireEvent.click(within(item).getByRole("button", { name: /Comment: My Session/ }));
    expect(item).toHaveAttribute("data-expanded", "true");
    expect(within(item).getByText("src/app.ts")).toBeInTheDocument();
    expect(within(item).getByText("Please reconsider this line.")).toBeInTheDocument();
    expect(within(item).getByRole("link", { name: /Open file/ })).toHaveAttribute(
      "href",
      "/c/sess_1?file=src%2Fapp.ts&comment=cm_1",
    );
  });

  it("shows the load-error banner and retries failed sources on click", async () => {
    // WHY: failed snapshot/comment fetches block the empty state and surface a
    // banner whose Retry button re-runs the failed comment queries.
    const retryFailed = vi.fn();
    vi.mocked(commentInboxHook.useCommentInbox).mockReturnValue(
      commentInboxStub({ failedCount: 2, retryFailed }),
    );
    renderPage();

    const banner = await screen.findByTestId("inbox-load-error");
    expect(within(banner).getByText(/Couldn.t load inbox items from 2/)).toBeInTheDocument();
    fireEvent.click(within(banner).getByRole("button", { name: /Retry/ }));
    expect(retryFailed).toHaveBeenCalled();
    // The error path also suppresses the empty state.
    expect(screen.queryByText("Nothing waiting on you")).not.toBeInTheDocument();
  });
});

describe("InboxPage unread sessions", () => {
  it("lists a session with unseen output, previewing its latest reply", async () => {
    // WHY: agents often finish with output that needs follow-up but raise no
    // approval; the inbox surfaces the same sessions the sidebar dots.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockResolvedValue({
      items: [assistantReply("Refactor finished; 3 files changed.")],
      hasMore: false,
    });
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    expect(item).toHaveAttribute("data-kind", "done");
    expect(within(item).getByText("Unread Session")).toBeInTheDocument();
    expect(
      await within(item).findByText(/Refactor finished; 3 files changed\./),
    ).toBeInTheDocument();
    expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledWith("sess_unread", { limit: 12 });
    expect(screen.getByText("1 unread")).toBeInTheDocument();
    expect(screen.queryByText("Nothing waiting on you")).not.toBeInTheDocument();
  });

  it("labels a failed unseen turn as an error", async () => {
    const row = unreadConversation({ status: "failed" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    expect(item).toHaveAttribute("data-kind", "error");
    expect(within(item).getByText("Error")).toBeInTheDocument();
  });

  it("leaves sessions the viewer has already read out of the inbox", async () => {
    const row = conversation({ id: "sess_read", status: "idle", pending_elicitations_count: 0 });
    seedReadState([{ id: row.id, viewer_last_seen: row.updated_at }]);
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    expect(await screen.findByText("Nothing waiting on you")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument();
  });

  it("expands to the session actions and drops the row on Mark as read", async () => {
    // WHY: marking read writes the shared read-state mirror, which the page
    // subscribes to — the row must clear at once, not on the next list poll.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    expect(item).toHaveAttribute("data-expanded", "false");
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    expect(item).toHaveAttribute("data-expanded", "true");
    expect(within(item).getByRole("link", { name: /Open session/ })).toHaveAttribute(
      "href",
      "/c/sess_unread",
    );

    fireEvent.click(within(item).getByRole("button", { name: /Mark as read/ }));
    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());
    expect(screen.getByText("Nothing waiting on you")).toBeInTheDocument();
  });

  it("clears an explicitly marked-unread session when opened from the inbox", async () => {
    // WHY: a freshly mounted chat keeps an explicit "Mark as unread" override,
    // so Open session must mark the session read itself or the row lingers.
    const row = conversation({
      id: "sess_flagged",
      title: "Flagged Session",
      status: "idle",
      pending_elicitations_count: 0,
    });
    seedReadState([{ id: row.id, viewer_last_seen: row.updated_at - 1, viewer_unread: true }]);
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    fireEvent.click(within(item).getByRole("button", { name: /Flagged Session/ }));
    fireEvent.click(within(item).getByRole("link", { name: /Open session/ }));

    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());
    expect(isExplicitlyUnread("sess_flagged")).toBe(false);
  });

  it("re-surfaces a row collapsed after it was expanded and marked read", async () => {
    // WHY: the expand toggle is dropped on read, so a later reply on the same
    // session lands collapsed like any new unread row.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = () => (
      <QueryClientProvider client={queryClient}>
        <SidebarDataProvider>
          <MemoryRouter>
            <InboxPage />
          </MemoryRouter>
        </SidebarDataProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(tree());

    const item = await screen.findByTestId("inbox-unread");
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    fireEvent.click(within(item).getByRole("button", { name: /Mark as read/ }));
    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());

    // A new reply lands after the read baseline (which is anchored at "now").
    const replied = { ...row, updated_at: Math.floor(Date.now() / 1000) + 60 };
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([replied]));
    rerender(tree());

    expect(await screen.findByTestId("inbox-unread")).toHaveAttribute("data-expanded", "false");
  });

  it("keeps the row usable when its preview fetch fails", async () => {
    // WHY: a missing preview isn't a load failure — the row still renders
    // without one, raises no error banner, and can be marked read.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockRejectedValue(new Error("boom"));
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    // The preview query retries once before settling into its error state.
    await waitFor(() => expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledTimes(2), {
      timeout: 3_000,
    });
    expect(item).toHaveTextContent("Unread Session");
    expect(item).not.toHaveTextContent("—");
    expect(screen.queryByTestId("inbox-load-error")).not.toBeInTheDocument();

    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    fireEvent.click(within(item).getByRole("button", { name: /Mark as read/ }));
    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());
  });
});

describe("InboxPage recency groups", () => {
  it("interleaves kinds newest first under recency headers", async () => {
    // WHY: items of every kind share one timeline, so a fresh reply sits
    // above an older approval — the approval still opens by default.
    const nowSeconds = Math.floor(Date.now() / 1000);
    const approvalRow = conversation({
      id: "sess_approval",
      title: "Approval Session",
      updated_at: nowSeconds - 3 * 86_400,
    });
    const unreadRow = unreadConversation({ updated_at: nowSeconds - 30 });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([approvalRow, unreadRow]),
    );
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    await screen.findByTestId("inbox-item");
    const regions = screen.getAllByRole("region");
    expect(regions.map((r) => r.getAttribute("aria-label"))).toEqual(["Just now", "This week"]);
    expect(within(regions[0]).getByTestId("inbox-unread")).toBeInTheDocument();
    expect(within(regions[1]).getByTestId("inbox-item")).toHaveAttribute("data-expanded", "true");
    // The header pill totals every kind; its label spells out the breakdown.
    expect(screen.getByText("1 approval · 1 unread")).toBeInTheDocument();
  });
});

describe("InboxPage filter tabs", () => {
  function renderMixedInbox() {
    const approvalRow = conversation({ id: "sess_approval", title: "Approval Session" });
    const unreadRow = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([approvalRow, unreadRow]),
    );
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", "Approve this?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();
  }

  it("shows every kind under All, then narrows to one kind per tab", async () => {
    renderMixedInbox();
    expect(await screen.findByTestId("inbox-item")).toBeInTheDocument();
    expect(screen.getByTestId("inbox-unread")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "All" })).toHaveAttribute("aria-selected", "true");

    selectTab("Awaiting response");
    expect(screen.getByTestId("inbox-item")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument();

    selectTab("Unread");
    expect(screen.getByTestId("inbox-unread")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-item")).not.toBeInTheDocument();
    // The header still totals every kind, so other tabs' contents stay visible.
    expect(screen.getByText("1 approval · 1 unread")).toBeInTheDocument();
  });

  it("restores the persisted tab on the next visit", async () => {
    renderMixedInbox();
    await screen.findByTestId("inbox-item");
    selectTab("Unread");
    expect(localStorage.getItem("omnigent:inbox-filter")).toBe("unread");
    cleanup();

    renderMixedInbox();
    expect(screen.getByRole("tab", { name: "Unread" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("inbox-unread")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-item")).not.toBeInTheDocument();
  });

  it("shows a tab-specific empty state", async () => {
    renderPage();
    await screen.findByText("Nothing waiting on you");
    selectTab("Unread");
    expect(screen.getByText("You’re all caught up")).toBeInTheDocument();
    selectTab("Awaiting response");
    expect(screen.getByText("No approvals waiting")).toBeInTheDocument();
  });
});
