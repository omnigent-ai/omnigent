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

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InboxPage } from "./InboxPage";
import type { Conversation } from "@/hooks/useConversations";
import * as conversationsHook from "@/hooks/useConversations";
import * as commentInboxHook from "@/hooks/useCommentInbox";
import * as sessionsApi from "@/lib/sessionsApi";
import type { CommentInbox } from "@/hooks/useCommentInbox";

// Minimal ApprovalCard stub: renders the message and an Accept button that
// forwards to the page's submit handler. The real card's form/preview UX is
// out of scope here — we only need to exercise `makeSubmit` → `approve`.
vi.mock("@/components/blocks/ApprovalCard", () => ({
  ApprovalCard: ({
    elicitationId,
    message,
    status,
    onSubmit,
  }: {
    elicitationId: string;
    message: string;
    status: string;
    onSubmit: (id: string, action: "accept" | "decline") => void;
  }) => (
    <div data-testid="approval-card" data-status={status}>
      <span>{message}</span>
      <button type="button" onClick={() => onSubmit(elicitationId, "accept")}>
        Stub Accept
      </button>
    </div>
  ),
}));

vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof import("@/hooks/useConversations")>()),
  useConversations: vi.fn(),
}));
vi.mock("@/hooks/useCommentInbox", () => ({ useCommentInbox: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ getSession: vi.fn(), approve: vi.fn() }));

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
      <MemoryRouter>
        <InboxPage />
      </MemoryRouter>
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
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

  it("does not show the empty state while more pages are still draining", () => {
    // WHY: `hasNextPage` keeps the inbox in the assembling state — an empty
    // `items` then only means "not done paging", so no empty state.
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([], { hasNextPage: true }),
    );
    renderPage();
    expect(screen.queryByText("Nothing waiting on you")).not.toBeInTheDocument();
    expect(screen.getByText("Loading inbox…")).toBeInTheDocument();
  });

  it("drains remaining list pages while mounted", () => {
    // WHY: an awaiting session may sit below the first page, so the inbox
    // calls fetchNextPage whenever another page is available.
    const fetchNextPage = vi.fn();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(
      conversationsStub([], { hasNextPage: true, fetchNextPage }),
    );
    renderPage();
    expect(fetchNextPage).toHaveBeenCalled();
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
    fireEvent.click(within(item).getByRole("button", { name: /My Session/ }));
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
    // WHY: the comment side of the inbox renders each unseen comment with its
    // author pill, file path, and body, and counts it in the header summary.
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

    expect(await screen.findByTestId("inbox-comment")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
    expect(screen.getByText("Please reconsider this line.")).toBeInTheDocument();
    expect(screen.getByText(/1 comment/)).toBeInTheDocument();
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
