import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
import { SidebarDataProvider } from "@/hooks/useSidebarData";
// Tests for the Inbox page (`/inbox`) — the cross-session list of pending
// approval prompts and unseen file comments.
//
// The page composes several live data sources, so we mock at their seams:
//  - `useConversations` (the session list + its paging drain),
//  - `useCommentInbox` (the comment side of the inbox),
//  - `getSession` / `approve` (per-session snapshot fetch + the verdict POST),
//  - `postEvent` (the reply POST).
// The pure assembly helper `collectInboxItems` and the display helpers are
// left REAL, so raw events follow the production parse path. Native delivery
// tests also use the real ApprovalCard; other prompts use an Accept-button stub.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { InboxPage } from "./InboxPage";
import type * as approvalCard from "@/components/blocks/ApprovalCard";
import type { ComponentProps } from "react";
import type { Conversation } from "@/hooks/useConversations";
import * as conversationsHook from "@/hooks/useConversations";
import * as commentInboxHook from "@/hooks/useCommentInbox";
import * as sessionsApi from "@/lib/sessionsApi";
import type { CommentInbox } from "@/hooks/useCommentInbox";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  resetReadStateForTests,
  seedReadState,
} from "@/hooks/useUnseenConversations";

// Keep real controls for native delivery and directory grants.
vi.mock("@/components/blocks/ApprovalCard", async (importActual) => {
  const actual = await importActual<typeof approvalCard>();
  return {
    ApprovalCard: (props: ComponentProps<typeof actual.ApprovalCard>) => {
      if (props.nativeDelivery || props.permissionDirectories)
        return <actual.ApprovalCard {...props} />;
      const { elicitationId, message, status, allowAutoMode, onSubmit, requester } = props;
      return (
        <div data-testid="approval-card" data-status={status}>
          <span>{message}</span>
          {requester && (
            <span data-testid="approval-card-requester">
              {requester.sessionId} under {requester.ancestorSessionId}
            </span>
          )}
          <button type="button" onClick={() => onSubmit!(elicitationId, "accept")}>
            Stub Accept
          </button>
          {allowAutoMode && (
            <button
              type="button"
              onClick={() => onSubmit!(elicitationId, "accept", { allow_auto_mode: true })}
            >
              Stub Auto Mode
            </button>
          )}
        </div>
      );
    },
  };
});

vi.mock("@/hooks/useConversations", async (importActual) => ({
  ...(await importActual<typeof conversationsHook>()),
  useConversations: vi.fn(),
}));
vi.mock("@/hooks/useCommentInbox", () => ({ useCommentInbox: vi.fn() }));
// Real exports (ApiError, codes) so failure handling sees genuine error types.
vi.mock("@/lib/sessionsApi", async (importActual) => ({
  ...(await importActual<typeof sessionsApi>()),
  getSession: vi.fn(),
  approve: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
  postEvent: vi.fn(),
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

function renderPage(
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  // Fresh client per render so cached queries never leak between tests.
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
  vi.mocked(sessionsApi.postEvent).mockResolvedValue({ queued: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetReadStateForTests();
  localStorage.clear();
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
      pendingElicitations: [rawElicitation("eli_1", "Claude wants to call **Bash**")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    const item = await screen.findByTestId("inbox-item");
    const toggle = within(item).getByRole("button", { name: /My Session/ });
    expect(toggle).toHaveClass("cursor-pointer");
    fireEvent.click(toggle);
    await waitFor(() => expect(item).toHaveAttribute("data-expanded", "false"));
    expect(screen.queryByTestId("approval-card")).not.toBeInTheDocument();
    // The collapsed row previews the same gating message with its bold tool
    // name, not the raw `**Bash**` markers.
    expect(within(toggle).getByText("Bash", { selector: "strong" })).toBeInTheDocument();
    expect(toggle.textContent).not.toContain("**");
  });

  it("previews a policy prompt's command verbatim in the collapsed row", async () => {
    // WHY: only the bridges' `**<tool>**` message is formatted; a balanced `**`
    // inside a command the user is approving must not be read as emphasis.
    const message = "Agent wants to call sys_os_shell('ls **/vendor/**'). Approve?";
    const row = conversation({ id: "sess_1" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_1", message)],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    const item = await screen.findByTestId("inbox-item");
    const toggle = within(item).getByRole("button", { name: /My Session/ });
    fireEvent.click(toggle);
    await waitFor(() => expect(item).toHaveAttribute("data-expanded", "false"));
    expect(toggle.textContent).toContain(message);
    expect(toggle.querySelector("strong")).toBeNull();
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

  it("shows directory access from a snapshot and submits only the user's consent", async () => {
    const row = conversation({ id: "sess_directory" });
    const directories = ["/worktrees/branch", "/mount/real-branch"];
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [
        rawElicitation("eli_directory", "Claude wants to call **Edit**", {
          permission_directories: directories,
        }),
      ],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    const button = await screen.findByRole("button", {
      name: "Approve & allow directories for this session",
    });
    for (const directory of directories) {
      expect(screen.getByText(directory, { selector: "code" })).toBeInTheDocument();
    }
    fireEvent.click(button);
    await waitFor(() =>
      expect(sessionsApi.approve).toHaveBeenCalledWith("sess_directory", "eli_directory", {
        action: "accept",
        content: { allow_directories: true },
      }),
    );
    expect(screen.getByText("Approved · directory access for this session")).toBeInTheDocument();
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
    // The card names the child, since the row it surfaced under is the parent.
    expect(screen.getByTestId("approval-card-requester")).toHaveTextContent("child under parent");
  });

  it("does not attribute a session's own prompt to a sub-agent", async () => {
    // WHY: only mirrored prompts carry a requester; a row's own prompt must
    // not read as coming from a sub-agent.
    const row = conversation({ id: "parent" });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.getSession).mockResolvedValue({
      pendingElicitations: [rawElicitation("eli_own", "Own approval?")],
    } as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>);
    renderPage();

    expect(await screen.findByRole("button", { name: "Stub Accept" })).toBeInTheDocument();
    expect(screen.queryByTestId("approval-card-requester")).toBeNull();
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

  it("shows the whole latest reply on hover when the preview cut it", async () => {
    // WHY: the preview keeps 3 lines and 280 characters, but the full reply is
    // already loaded, so hovering shows the rest without another request.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    const reply = ["First line.", "Second line.", "Third line.", "**Fourth** has the verdict."];
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockResolvedValue({
      items: [assistantReply(reply.join("\n"))],
      hasMore: false,
    });
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    const preview = await within(item).findByText(/Third line\./);
    expect(preview).toHaveAttribute("tabindex", "0");
    expect(screen.queryByText(/has the verdict/)).not.toBeInTheDocument();

    fireEvent.pointerEnter(preview);

    // Rendered like the chat: the markdown bold renders as bold, not asterisks.
    expect(await screen.findByText("Fourth")).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText(/has the verdict/)).toBeInTheDocument();
    expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledTimes(1);
  });

  it("offers no hover card when the preview already shows the whole reply", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockResolvedValue({
      items: [assistantReply("All done.\n\nNothing else to report.")],
      hasMore: false,
    });
    renderPage();

    const item = await screen.findByTestId("inbox-unread");
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    const preview = await within(item).findByText(/Nothing else to report\./);
    expect(preview).not.toHaveAttribute("data-slot", "hover-card-trigger");
    expect(preview).not.toHaveAttribute("tabindex");
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

describe("InboxPage replies", () => {
  function expandUnread(): HTMLElement {
    const item = screen.getByTestId("inbox-unread");
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    return item;
  }

  /** Types a reply and sends it once the preview has loaded and Send is live. */
  async function typeAndSend(item: HTMLElement, text: string): Promise<HTMLElement> {
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: text } });
    const send = within(item).getByRole("button", { name: "Send" });
    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.click(send);
    return box;
  }

  /** Renders the page and returns a hook that pushes refetched session rows. */
  function renderWithRows(initial: Conversation[]): (rows: Conversation[]) => void {
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
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub(initial));
    const { rerender } = render(tree());
    return (rows) => {
      vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub(rows));
      rerender(tree());
    };
  }

  function sessionItems(...items: unknown[]) {
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockResolvedValue({
      items: items as Awaited<ReturnType<typeof sessionsApi.fetchSessionItemsPage>>["items"],
      hasMore: false,
    });
  }

  /** Sends "Ship it" from a Done row whose preview shows "Refactor done.". */
  async function sendFromDoneRow(initial: Conversation) {
    sessionItems({ ...assistantReply("Refactor done."), id: "item_done" });
    const pushRows = renderWithRows([initial]);
    await within(await screen.findByTestId("inbox-unread")).findByText(/Refactor done\./);
    await typeAndSend(expandUnread(), "Ship it");
    await within(screen.getByTestId("inbox-unread")).findByRole("status");
    return pushRows;
  }

  it("posts the reply as a user message and waits for the agent's answer", async () => {
    const toasts: unknown[] = [];
    const onToast = (e: Event) => toasts.push((e as CustomEvent).detail.content);
    window.addEventListener("omnigent:toast", onToast);
    onTestFinished(() => window.removeEventListener("omnigent:toast", onToast));
    const row = unreadConversation();
    sessionItems(assistantReply("Refactor done."));
    renderWithRows([row]);

    await within(await screen.findByTestId("inbox-unread")).findByText(/Refactor done\./);
    const item = expandUnread();
    expect(within(item).getByRole("button", { name: "Send" })).toBeDisabled();
    await typeAndSend(item, "Ship it");

    expect(await within(item).findByRole("status")).toHaveTextContent("Reply sent.");
    expect(within(item).queryByRole("textbox")).not.toBeInTheDocument();
    expect(sessionsApi.postEvent).toHaveBeenCalledWith(
      "sess_unread",
      {
        type: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: "Ship it" }],
          stable_id: expect.any(String),
        },
      },
      { holdWhenHostOffline: true },
    );
    expect(toasts).toEqual(["Reply sent to Unread Session"]);
    // The baseline didn't move: only the session's next output decides.
    expect(isConversationUnseen(row.id, row.updated_at, row.status)).toBe(true);
  });

  it("brings the row back when the replied turn fails in the same second", async () => {
    // WHY: `updated_at` has whole-second precision, so a baseline moved to
    // "now" on send would read a failure recorded in that second as seen.
    const row = unreadConversation();
    const pushRows = await sendFromDoneRow(row);

    sessionItems(
      { ...assistantReply("Refactor done."), id: "item_done" },
      { id: "item_failed", type: "error", message: "The host is offline." },
    );
    pushRows([{ ...row, status: "failed", updated_at: Math.floor(Date.now() / 1000) }]);

    const failed = screen.getByTestId("inbox-unread");
    await waitFor(() => expect(failed).toHaveAttribute("data-kind", "error"));
    expect(await within(failed).findByText("The host is offline.")).toBeInTheDocument();
    expect(within(failed).queryByRole("status")).not.toBeInTheDocument();
    expect(within(failed).getByRole("textbox", { name: /Reply to/ })).toHaveValue("");
  });

  it("shows an answer that lands in the second whose preview was already fetched", async () => {
    // WHY: persisting the reply bumps `updated_at` before the runner reports
    // running, and the preview for that second is cached with the old output.
    // An answer in the same second must still replace it.
    const row = unreadConversation();
    const pushRows = await sendFromDoneRow(row);
    const replySecond = row.updated_at + 1;

    pushRows([{ ...row, updated_at: replySecond }]);
    await waitFor(() => expect(sessionsApi.fetchSessionItemsPage).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(within(screen.getByTestId("inbox-unread")).getByRole("status")).toHaveTextContent(
      "Reply sent.",
    );

    pushRows([{ ...row, status: "running", updated_at: replySecond }]);
    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());

    sessionItems(
      { ...assistantReply("Refactor done."), id: "item_done" },
      { ...assistantReply("Shipped."), id: "item_answer" },
    );
    pushRows([{ ...row, status: "idle", updated_at: replySecond }]);
    const answered = await screen.findByTestId("inbox-unread");
    expect(await within(answered).findByText("Shipped.")).toBeInTheDocument();
    expect(within(answered).queryByRole("status")).not.toBeInTheDocument();
    expect(within(answered).getByRole("textbox", { name: /Reply to/ })).toHaveValue("");
  });

  it("shows a same-second answer even when the running turn is never seen", async () => {
    // WHY: with no status change and no new `updated_at`, nothing would
    // refetch the preview; the post-send re-check catches the answer.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    onTestFinished(() => {
      vi.useRealTimers();
    });
    const row = unreadConversation();
    await sendFromDoneRow(row);

    sessionItems(
      { ...assistantReply("Refactor done."), id: "item_done" },
      { ...assistantReply("Shipped."), id: "item_answer" },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    const item = screen.getByTestId("inbox-unread");
    expect(await within(item).findByText("Shipped.")).toBeInTheDocument();
    expect(within(item).queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps the sent note when a preview refetch fails", async () => {
    // WHY: a failed fetch carries no output, so it says nothing about an answer.
    const row = unreadConversation();
    const pushRows = await sendFromDoneRow(row);
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockRejectedValue(new Error("boom"));
    const callsBefore = vi.mocked(sessionsApi.fetchSessionItemsPage).mock.calls.length;

    pushRows([{ ...row, updated_at: row.updated_at + 1 }]);
    // The preview query retries once before settling into its error state.
    await waitFor(
      () =>
        expect(vi.mocked(sessionsApi.fetchSessionItemsPage).mock.calls.length).toBe(
          callsBefore + 2,
        ),
      { timeout: 3_000 },
    );
    await act(async () => {});
    expect(within(screen.getByTestId("inbox-unread")).getByRole("status")).toHaveTextContent(
      "Reply sent.",
    );
  });

  it("waits on the turn after replying from an Error row", async () => {
    const row = unreadConversation({ status: "failed" });
    sessionItems({ id: "item_failed", type: "error", message: "Model overloaded" });
    renderWithRows([row]);

    const item = await screen.findByTestId("inbox-unread");
    expect(item).toHaveAttribute("data-kind", "error");
    await within(item).findByText(/Model overloaded/);
    fireEvent.click(within(item).getByRole("button", { name: /Unread Session/ }));
    await typeAndSend(item, "Try a smaller model");

    // The row is still failed until the new turn runs; that alone settles nothing.
    expect(await within(item).findByRole("status")).toHaveTextContent("Reply sent.");
  });

  it("drops the sent note when the turn fails without writing an error item", async () => {
    const row = unreadConversation();
    const pushRows = await sendFromDoneRow(row);

    pushRows([{ ...row, status: "failed" }]);

    const failed = screen.getByTestId("inbox-unread");
    await waitFor(() => expect(failed).toHaveAttribute("data-kind", "error"));
    expect(within(failed).queryByRole("status")).not.toBeInTheDocument();
    expect(within(failed).getByRole("textbox", { name: /Reply to/ })).toBeInTheDocument();
  });

  it("holds Send until the row's preview has loaded", async () => {
    // WHY: the send records the current output to tell the answer from it.
    let loadPreview!: (page: { items: never[]; hasMore: boolean }) => void;
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockReturnValueOnce(
      new Promise((resolve) => {
        loadPreview = resolve;
      }),
    );
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "Ship it" } });
    const send = within(item).getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    await act(async () => loadPreview({ items: [], hasMore: false }));
    await waitFor(() => expect(send).toBeEnabled());
  });

  it("won't send until a preview loads, even after the preview fetch fails", async () => {
    // WHY: a failed fetch carries no baseline; sending anyway would record
    // `null` and read the next fetch's old reply as the agent's answer.
    const row = unreadConversation();
    vi.mocked(sessionsApi.fetchSessionItemsPage).mockRejectedValue(new Error("boom"));
    const pushRows = renderWithRows([row]);

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "Ship it" } });
    // The preview query retries once before settling into its error state.
    expect(
      await within(item).findByText(/replying here is paused/, {}, { timeout: 3_000 }),
    ).toBeInTheDocument();
    const send = within(item).getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(send);
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    // A later successful fetch supplies the baseline.
    sessionItems({ ...assistantReply("Refactor done."), id: "item_done" });
    pushRows([{ ...row, updated_at: row.updated_at + 1 }]);
    await waitFor(() => expect(send).toBeEnabled());
    expect(within(item).queryByText(/replying here is paused/)).not.toBeInTheDocument();
    fireEvent.click(send);
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1));
  });

  it("sends on Enter and leaves Shift+Enter to the textarea", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "Line one" } });
    await waitFor(() => expect(within(item).getByRole("button", { name: "Send" })).toBeEnabled());
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1));
  });

  it("sends only on Mod+Enter when that preference is on", async () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "Ship it" } });
    await waitFor(() => expect(within(item).getByRole("button", { name: "Send" })).toBeEnabled());
    fireEvent.keyDown(box, { key: "Enter" });
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1));
  });

  it("never sends on Enter with a touch pointer", async () => {
    const realMatchMedia = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query === "(pointer: coarse)",
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    onTestFinished(() => {
      window.matchMedia = realMatchMedia;
    });
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "Ship it" } });
    const send = within(item).getByRole("button", { name: "Send" });
    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.keyDown(box, { key: "Enter" });
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    fireEvent.click(send);
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1));
  });

  it("inserts a newline on Alt+Enter instead of sending", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const box = within(expandUnread()).getByRole<HTMLTextAreaElement>("textbox", {
      name: /Reply to/,
    });
    fireEvent.change(box, { target: { value: "firstsecond" } });
    box.setSelectionRange(5, 5);

    expect(fireEvent.keyDown(box, { key: "Enter", altKey: true })).toBe(false);
    expect(box).toHaveValue("first\nsecond");
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();
  });

  it("never sends the Enter that confirms an IME candidate", async () => {
    // WHY: Safari confirms a candidate with an Enter that isn't flagged as
    // composing (keyCode 229), which would send a half-typed reply.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = within(item).getByRole("textbox", { name: /Reply to/ });
    fireEvent.change(box, { target: { value: "にほんご" } });
    await waitFor(() => expect(within(item).getByRole("button", { name: "Send" })).toBeEnabled());

    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    fireEvent.compositionStart(box);
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.compositionEnd(box);
    expect(sessionsApi.postEvent).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1));
  });

  it("locks the reply while it sends, keeping focus and refusing a second send", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent).mockReturnValueOnce(new Promise(() => {}));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = await typeAndSend(item, "Ship it");

    expect(box).toHaveAttribute("readonly");
    expect(box).not.toBeDisabled();
    const send = within(item).getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    // Leaving would drop reply state the pending POST is about to write.
    expect(within(item).getByRole("button", { name: /Mark as read/ })).toBeDisabled();
    expect(within(item).getByRole("link", { name: /Open session/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.click(send);
    // Alt+Enter's fallback edits even a read-only box; it must not unlock it.
    expect(fireEvent.keyDown(box, { key: "Enter", altKey: true })).toBe(false);
    fireEvent.change(box, { target: { value: "Ship it twice" } });
    expect(box).toHaveValue("Ship it");
    expect(send).toBeDisabled();
    expect(sessionsApi.postEvent).toHaveBeenCalledTimes(1);
  });

  it("keeps the draft and the row when the reply is refused", async () => {
    // WHY: a failed send has no server-side record, so the inbox must hand
    // the text back rather than drop it with the row.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent).mockRejectedValueOnce(
      new Error("No host can restart this session's runner, so your message was not sent."),
    );
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = await typeAndSend(item, "Try again");

    expect(await within(item).findByRole("alert")).toHaveTextContent(
      "No host can restart this session's runner",
    );
    expect(box).toHaveValue("Try again");
    expect(box).not.toHaveAttribute("readonly");
    expect(isConversationUnseen(row.id, row.updated_at, row.status)).toBe(true);

    // Editing the draft clears the stale error.
    fireEvent.change(box, { target: { value: "Try again now" } });
    expect(within(item).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says the reply wasn't sent when the session's host is offline", async () => {
    // WHY: the server's hold message promises a resend that only the chat
    // composer performs.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent).mockRejectedValueOnce(
      new sessionsApi.ApiError(
        "The host for this session is offline. Your message will be sent when it reconnects.",
        503,
        sessionsApi.HOST_OFFLINE_CODE,
      ),
    );
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = await typeAndSend(item, "Wake up");

    const alert = await within(item).findByRole("alert");
    expect(alert).toHaveTextContent(
      "The host for this session is offline, so the reply wasn’t sent.",
    );
    expect(alert).not.toHaveTextContent("will be sent when it reconnects");
    expect(box).toHaveValue("Wake up");
  });

  it("retries an untouched failed reply under the same stable id, an edited one under a new id", async () => {
    // WHY: an untouched retry reuses the id so the server dedupes a reply that
    // already landed, after a lost response or a refusal after saving.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(
        new sessionsApi.ApiError("The runner didn't come online", 503, "runner_unavailable"),
      )
      .mockRejectedValueOnce(new Error("still down"))
      .mockResolvedValueOnce({ queued: true });
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = await typeAndSend(item, "Ship it");
    expect(await within(item).findByRole("alert")).toHaveTextContent("Failed to fetch");
    fireEvent.click(within(item).getByRole("button", { name: "Send" }));
    expect(await within(item).findByRole("alert")).toHaveTextContent(
      "The runner didn't come online",
    );
    fireEvent.click(within(item).getByRole("button", { name: "Send" }));
    expect(await within(item).findByRole("alert")).toHaveTextContent("still down");
    fireEvent.change(box, { target: { value: "Ship it now" } });
    fireEvent.click(within(item).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sessionsApi.postEvent).toHaveBeenCalledTimes(4));

    const stableIds = vi
      .mocked(sessionsApi.postEvent)
      .mock.calls.map(([, event]) => (event.data as { stable_id: string }).stable_id);
    expect(stableIds[1]).toBe(stableIds[0]);
    expect(stableIds[2]).toBe(stableIds[0]);
    expect(stableIds[3]).not.toBe(stableIds[0]);
  });

  it("settles a successful retry at once, since the server may have deduped it", async () => {
    // WHY: a retry that matches a send that already ran starts no new turn,
    // so a "Reply sent" note would never clear.
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ queued: true });
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    await typeAndSend(item, "Ship it");
    await within(item).findByRole("alert");
    fireEvent.click(within(item).getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(within(item).getByRole("textbox", { name: /Reply to/ })).toHaveValue(""),
    );
    expect(within(item).queryByRole("status")).not.toBeInTheDocument();
    expect(within(item).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports a policy-denied reply and keeps the draft and the row", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    vi.mocked(sessionsApi.postEvent).mockResolvedValueOnce({ queued: false, denied: true });
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    const box = await typeAndSend(item, "rm -rf /");

    expect(await within(item).findByRole("alert")).toHaveTextContent(
      "A policy blocked this reply.",
    );
    expect(box).toHaveValue("rm -rf /");
    expect(screen.getByTestId("inbox-unread")).toBeInTheDocument();
  });

  it("keeps a draft across collapsing and re-expanding the card", async () => {
    const row = unreadConversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    fireEvent.change(within(item).getByRole("textbox", { name: /Reply to/ }), {
      target: { value: "Half-written" },
    });
    const toggle = within(item).getByRole("button", { name: /Unread Session/ });
    fireEvent.click(toggle);
    expect(within(item).queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(toggle);

    expect(within(item).getByRole("textbox", { name: /Reply to/ })).toHaveValue("Half-written");
  });

  it.each([
    ["Mark as read", "button"],
    ["Open session", "link"],
  ] as const)("drops a stale draft and error once %s clears the row", async (name, role) => {
    const row = unreadConversation();
    vi.mocked(sessionsApi.postEvent).mockRejectedValueOnce(new Error("boom"));
    const pushRows = renderWithRows([row]);

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    await typeAndSend(item, "Never mind");
    await within(item).findByRole("alert");
    fireEvent.click(within(item).getByRole(role, { name: new RegExp(name) }));
    await waitFor(() => expect(screen.queryByTestId("inbox-unread")).not.toBeInTheDocument());

    pushRows([{ ...row, updated_at: Math.floor(Date.now() / 1000) + 60 }]);
    await screen.findByTestId("inbox-unread");
    const resurfaced = expandUnread();
    expect(within(resurfaced).getByRole("textbox", { name: /Reply to/ })).toHaveValue("");
    expect(within(resurfaced).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("undoes an explicit Mark as unread when a reply is sent", async () => {
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
    await typeAndSend(item, "Ship it");

    await within(item).findByRole("status");
    expect(isExplicitlyUnread("sess_flagged")).toBe(false);
  });

  it("offers no reply box on a session shared read-only", async () => {
    // WHY: the server refuses user messages below edit access.
    const row = unreadConversation({ permission_level: 1 });
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    renderPage();

    await screen.findByTestId("inbox-unread");
    const item = expandUnread();
    expect(within(item).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(item).queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(within(item).getByRole("link", { name: /Open session/ })).toBeInTheDocument();
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

it.each(
  ["success", "failure"].flatMap((outcome) =>
    [false, true].map((retryFirst) => ({ outcome, retryFirst })),
  ),
)(
  "adopts a newer Inbox attempt during a stalled POST ($outcome, retry first: $retryFirst)",
  async ({ outcome, retryFirst }) => {
    const row = conversation();
    vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
    let delivery = { state: "ready", attempt: "first", action: "accept" };
    vi.mocked(sessionsApi.getSession).mockImplementation(
      async () =>
        ({
          pendingElicitations: [
            rawElicitation("eli_1", "Approve pwd", { native_delivery: delivery }),
          ],
        }) as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>,
    );
    const submissions: {
      resolve: (value: Awaited<ReturnType<typeof sessionsApi.approve>>) => void;
      reject: (error: Error) => void;
    }[] = [];
    vi.mocked(sessionsApi.approve).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          submissions.push({ resolve, reject });
        }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const poll = () =>
      act(async () => {
        await queryClient.refetchQueries({ queryKey: ["inbox-elicitations"] });
      });
    renderPage(queryClient);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await poll();
    expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
    delivery = { state: "unconfirmed", attempt: "second", action: "accept" };
    await poll();
    const retry = await screen.findByRole("button", { name: "Retry sending approval" });
    expect(retry).toBeEnabled();
    if (retryFirst) fireEvent.click(retry);
    await act(async () => {
      if (outcome === "failure") submissions[0]!.reject(new Error("offline"));
      else submissions[0]!.resolve({} as Awaited<ReturnType<typeof sessionsApi.approve>>);
    });
    if (!retryFirst)
      fireEvent.click(screen.getByRole("button", { name: "Retry sending approval" }));
    await poll();
    expect(screen.getByText("Waiting for Kiro to confirm")).toBeInTheDocument();
    expect(submissions).toHaveLength(2);
    expect(sessionsApi.approve).toHaveBeenLastCalledWith("sess_1", "eli_1", {
      action: "accept",
      _meta: { native_delivery_attempt: "second" },
    });
    await act(async () => {
      submissions[1]!.reject(new Error("retry failed"));
    });
    expect(await screen.findByRole("button", { name: "Retry sending approval" })).toBeEnabled();
  },
);

it("recovers a native request after an empty snapshot without reloading", async () => {
  vi.useFakeTimers();
  const row = conversation();
  vi.mocked(conversationsHook.useConversations).mockReturnValue(conversationsStub([row]));
  const pending = [
    rawElicitation("eli_1", "Approve pwd", {
      native_delivery: { state: "unconfirmed", attempt: "recovered", action: "accept" },
    }),
  ];
  let pendingElicitations = pending;
  vi.mocked(sessionsApi.getSession).mockImplementation(
    async () =>
      ({ pendingElicitations }) as unknown as Awaited<ReturnType<typeof sessionsApi.getSession>>,
  );
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 30_000, refetchOnWindowFocus: false } },
  });
  const poll = () =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(1001);
    });
  try {
    await act(async () => {
      renderPage(queryClient);
    });
    await poll();
    expect(screen.getByRole("button", { name: "Retry sending approval" })).toBeVisible();
    pendingElicitations = [];
    await poll();
    expect(
      screen.queryByRole("button", { name: "Retry sending approval" }),
    ).not.toBeInTheDocument();
    pendingElicitations = pending;
    await poll();
    expect(screen.getByRole("button", { name: "Retry sending approval" })).toBeVisible();
    expect(sessionsApi.approve).not.toHaveBeenCalled();
  } finally {
    cleanup();
    queryClient.clear();
    vi.useRealTimers();
  }
});
