import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestsModule from "@/newPullRequestPage/hooks/usePullRequests";
import type {
  PullRequestComment,
  PullRequestReview,
  PullRequestTimelineEvent,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { usePullRequestReviewThreads } from "@/newPullRequestPage/hooks/usePullRequestReviewThreads";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { PaletteCommandScope } from "@/store/PaletteCommandScope";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { commentsOrderPreference } from "@/pullRequestComments/commentsOrder";

import { type ActivityRevealRequest, useActivityRevealRequest } from "./activityReveal";
import { postedCommentKey } from "./activityKeys";
import { PullRequestActivity, type PullRequestActivityProps } from "./PullRequestActivity";

// The reply editor belongs to the composer; here it only has to appear where replies are allowed.
vi.mock("@/newPullRequestPage/shell/PullRequestCommentComposer", () => ({
  PullRequestCommentForm: () => <form aria-label="Reply to this conversation" />,
}));
// The box's pending review controls are the composer's concern; this page only shows them.
vi.mock("@/pullRequestReviews/usePendingComposer", () => ({ usePendingReply: () => null }));
// Resolving needs this Mac; these pages read through the host.
// Reactions are their own concern; nothing here offers them.
vi.mock("@/pullRequestComments/useCommentReactions", () => ({ useCommentReactions: () => null }));
vi.mock("@/pullRequestComments/useResolutionHere", () => ({
  useResolutionHere: () => ({
    command: null,
    focus: { active: false, none: true, markFocused: () => {}, release: () => {} },
  }),
}));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestsModule>()),
  usePullRequestCommentComposer: () => ({
    draft: { body: "", pending: false },
    setBody: vi.fn(),
    post: vi.fn(),
  }),
  usePullRequestCommentQuote: () => ({ quote: vi.fn() }),
}));
// Editing and deleting need this Mac; these pages read through the host.
vi.mock("@/pullRequestComments/usePullRequestCommentEditing", () => ({
  usePullRequestCommentEditing: () => ({
    actions: null,
    entry: {},
    setText: vi.fn(),
    deleteAsked: false,
    setDeleteAsked: vi.fn(),
    confirmDelete: vi.fn(),
    discardAsked: false,
    setDiscardAsked: vi.fn(),
    confirmDiscard: vi.fn(),
    checkNeedsLogin: null,
  }),
}));
// Markdown rendering is the shared renderer's concern; bodies only need to be readable here.
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({
    children,
    rehypePluginsAfterSanitizing,
  }: {
    children: string;
    rehypePluginsAfterSanitizing?: readonly unknown[];
  }) => (
    <div data-github-markdown={rehypePluginsAfterSanitizing?.length ? "on" : undefined}>
      {children}
    </div>
  ),
  ChatCodeBlockPre: () => null,
}));
vi.mock("@/newPullRequestPage/shell/PullRequestReviewBody", () => ({
  PullRequestReviewBody: ({ body }: { body: string }) => <div>{body}</div>,
}));

const HUNK = [
  "@@ -1,4 +1,273 @@",
  ...Array.from({ length: 269 }, (_, index) => `+line ${index + 1}`),
  '+      tracing::warn!("closed an edge connection");',
  "+      return;",
  "+    }",
  "+    let _ = connections.send((tls, addr)).await;",
].join("\n");

function thread(index: number, over: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id: `thread-${index}`,
    path: index % 2 ? "devtools/server/src/edge.rs" : "devtools/server/src/media/objects.rs",
    side: "RIGHT",
    line: null,
    start_line: null,
    original_line: 273,
    original_start_line: null,
    subject_type: "LINE",
    resolved: true,
    outdated: true,
    can_reply: true,
    diff_hunk: HUNK,
    comments: {
      data: [
        {
          id: `comment-${index}`,
          author: "reviewer-bot",
          body: `Please keep this off the tunnel loop (${index})`,
          created_at: new Date(Date.UTC(2026, 9, 20, 10, index)).toISOString(),
          url: `https://github.com/acme/universe/pull/2828085#discussion_r${index}`,
        },
      ],
      has_more: false,
      next_cursor: null,
    },
    ...over,
  };
}

const CONVERSATION: PullRequestComment = {
  author: "example-ci-bot",
  body: "Merge Requirements\n\nPR is NOT ready for merge.",
  created_at: "2026-10-20T09:00:00Z",
  url: "https://github.com/acme/universe/pull/2828085#issuecomment-1",
};

function submitted(
  id: string,
  author: string,
  state: PullRequestReview["state"],
  body: string,
  minute: number,
): PullRequestReview {
  return {
    id,
    author,
    state,
    body,
    submitted_at: new Date(Date.UTC(2026, 9, 21, 9, minute)).toISOString(),
    url: `https://github.com/acme/universe/pull/2828085#pullrequestreview-${id}`,
  };
}

// An empty approval, changes requested with a body, and a dismissed review.
const SUBMITTED = [
  submitted("r-ok", "ada", "APPROVED", "", 1),
  submitted("r-changes", "ada", "CHANGES_REQUESTED", "Please split the migration out", 2),
  submitted("r-gone", "ben", "DISMISSED", "", 3),
];

type Reviews = ReturnType<typeof usePullRequestReviewThreads>;

function reviews(
  over: Partial<Reviews> = {},
  threads = Array.from({ length: 8 }, (_, i) => thread(i)),
): Reviews {
  return {
    page: {
      data: threads,
      has_more: false,
      next_cursor: null,
      head_sha: "a".repeat(40),
      base_sha: "b".repeat(40),
    },
    isFetching: false,
    historyStale: false,
    refreshHistory: vi.fn(),
    retry: vi.fn(),
    loadMore: vi.fn(),
    loadingMore: false,
    loadComments: vi.fn(),
    loadingComments: null,
    error: undefined,
    ...over,
  } as unknown as Reviews;
}

const CONTEXT: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: "https://github.com/acme/universe/pull/2828085",
  account: "octocat",
  canReply: true,
  loadComments: vi.fn(),
  loadingComments: null,
};

const PR_URL = "https://github.com/acme/universe/pull/2828085";

function push(id: string, minute: number, over: Partial<PullRequestTimelineEvent> = {}) {
  return {
    id,
    kind: "commit",
    author: "olaf",
    created_at: new Date(Date.UTC(2026, 9, 21, 10, minute)).toISOString(),
    url: null,
    sha: `${id}${"0".repeat(39)}`.slice(0, 40),
    message: `Push ${id}`,
    ...over,
  } satisfies PullRequestTimelineEvent;
}

function renderPage(over: Partial<PullRequestActivityProps> = {}) {
  const props: PullRequestActivityProps = {
    active: true,
    comments: [CONVERSATION],
    commentsPartial: false,
    events: [],
    eventsPartial: false,
    submittedReviews: null,
    submittedReviewsPartial: false,
    reviews: reviews(),
    threadsAvailable: true,
    context: CONTEXT,
    providerLabel: "GitHub",
    pullRequestAuthor: "olaf",
    pullRequestUrl: PR_URL,
    revealRequest: null,
    onRevealSettled: vi.fn(),
    ...over,
  };
  const view = render(<PullRequestActivity {...props} />);
  return { ...view, props };
}

const pill = (name: RegExp) =>
  within(screen.getByRole("navigation", { name: "Show" })).getByRole("button", { name });
const threads = () => screen.queryAllByRole("region", { name: /^Conversation on / });
const conversation = () => screen.queryByRole("article", { name: /example-ci-bot/ });
/** For tests that pick threads by their position in the loaded, oldest-first order. */
const listOldestFirst = () => commentsOrderPreference.write("oldest");
const commands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const command = (label: string) => commands().find((candidate) => candidate.label === label);

async function chooseOption(
  user: ReturnType<typeof userEvent.setup>,
  menu: string,
  item: RegExp,
  role: "menuitemradio" | "menuitemcheckbox" = "menuitemcheckbox",
) {
  await user.click(screen.getByRole("button", { name: "Activity options" }));
  screen.getByRole("menuitem", { name: menu }).focus();
  await user.keyboard("{ArrowRight}");
  await user.click(await screen.findByRole(role, { name: item }));
}

async function chooseThreads(user: ReturnType<typeof userEvent.setup>, state: RegExp) {
  await user.click(screen.getByRole("button", { name: "Thread state" }));
  await user.click(screen.getByRole("menuitemradio", { name: state }));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  usePaletteCommandsStore.setState({ groups: {} });
  Element.prototype.scrollIntoView = vi.fn();
});

describe("PullRequestActivity", () => {
  it("lists the conversation with every resolved thread, as the reference shows universe#2828085", () => {
    renderPage({
      reviews: reviews(
        {},
        Array.from({ length: 11 }, (_, i) => thread(i)),
      ),
    });
    expect(screen.getByRole("heading", { name: "Activity" })).toBeVisible();
    expect(pill(/^All/)).toHaveTextContent("All12");
    expect(pill(/^Comments/)).toHaveTextContent("Comments1");
    expect(pill(/^Threads/)).toHaveTextContent("Threads11");
    // The ten newest show, newest first; the rest are a click away.
    expect(threads()).toHaveLength(10);
    expect(conversation()).toBeNull();

    const first = threads()[0]!;
    expect(first).toHaveTextContent("objects.rs");
    expect(within(first).getByText("Line 273")).toBeVisible();
    expect(within(first).getByText("Outdated")).toBeVisible();
    expect(within(first).getByText("Resolved")).toBeVisible();
    // The hunk ends at the commented line, the rest a click away.
    expect(within(first).getByRole("button", { name: "Show 269 more lines" })).toBeVisible();
    // Resolved threads fold to one line until opened.
    const fold = within(first).getByRole("button", { name: /Resolved conversation/ });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(fold).toHaveTextContent("1 comment");
    expect(within(first).queryByText(/Please keep this off/)).toBeNull();
  });

  it("shows earlier entries on request, at the end of newest first and the start of oldest first", async () => {
    const user = userEvent.setup();
    const many = reviews(
      {},
      Array.from({ length: 11 }, (_, i) => thread(i)),
    );
    const view = renderPage({ reviews: many });
    await user.click(screen.getByRole("button", { name: "Show 2 earlier" }));
    expect(threads()).toHaveLength(11);
    expect(conversation()).toBeVisible();
    // Comment bodies render GitHub's emoji shortcodes, as the panel's do.
    expect(screen.getByText(/PR is NOT ready for merge/)).toHaveAttribute(
      "data-github-markdown",
      "on",
    );
    view.unmount();

    listOldestFirst();
    renderPage({ reviews: many });
    const earlier = screen.getByRole("button", { name: "Show 2 earlier" });
    expect(
      earlier.compareDocumentPosition(screen.getByRole("list", { name: "Activity" })) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(conversation()).toBeNull();
  });

  it("opens a resolved thread and the rest of its hunk", async () => {
    listOldestFirst();
    const user = userEvent.setup();
    renderPage();
    const first = threads()[0]!;
    await user.click(within(first).getByRole("button", { name: /Resolved conversation/ }));
    expect(within(first).getByText("Please keep this off the tunnel loop (0)")).toBeVisible();
    expect(within(first).getByRole("form", { name: "Reply to this conversation" })).toBeVisible();
    await user.click(within(first).getByRole("button", { name: "Show 269 more lines" }));
    expect(within(first).getByText("line 1")).toBeVisible();
  });

  it("shows one kind at a time from its pill, each with its count", async () => {
    const user = userEvent.setup();
    renderPage({ events: [push("p1", 1)] });
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^Pushes/)).toHaveTextContent("Pushes1");
    await user.click(pill(/^Comments/));
    expect(pill(/^Comments/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "false");
    expect(conversation()).toBeVisible();
    expect(threads()).toHaveLength(0);
    await user.click(pill(/^Pushes/));
    expect(screen.getByRole("group", { name: /^olaf pushed p100000/ })).toBeVisible();
    expect(conversation()).toBeNull();
    await user.click(pill(/^Threads/));
    expect(threads()).toHaveLength(8);
  });

  it("narrows threads to open or resolved ones from the Threads pill's menu, saying how many are open", async () => {
    const user = userEvent.setup();
    renderPage({
      reviews: reviews({}, [thread(0), thread(1, { resolved: false }), thread(2)]),
    });
    expect(pill(/^Threads/)).toHaveTextContent("Threads3 · 1 open");
    await chooseThreads(user, /^Open/);
    expect(pill(/^Threads/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^Threads/)).toHaveTextContent("Threads · Open1");
    expect(threads()).toHaveLength(1);
    expect(conversation()).toBeNull();
    await chooseThreads(user, /^Resolved/);
    expect(threads()).toHaveLength(2);
    await user.click(pill(/^All/));
    expect(threads()).toHaveLength(3);
    expect(conversation()).toBeVisible();
  });

  it("says when a narrowed timeline is empty, with a way back", async () => {
    const user = userEvent.setup();
    renderPage();
    await chooseThreads(user, /^Open/);
    expect(screen.getByText("Nothing matches")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear Filters" }));
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(threads()).toHaveLength(8);
  });

  it("lists pushes, which open their commit, and leaves the Pushes pill out where pushes weren't read", async () => {
    const user = userEvent.setup();
    const openCommit = vi.fn();
    const view = renderPage({ events: [push("p1", 1)], openCommit });
    await user.click(
      screen.getByRole("button", { name: /^olaf pushed p100000.*open on Commits$/ }),
    );
    expect(openCommit).toHaveBeenCalledWith(push("p1", 1).sha);
    view.rerender(<PullRequestActivity {...view.props} events={null} />);
    expect(
      within(screen.getByRole("navigation", { name: "Show" })).queryByRole("button", {
        name: /^Pushes/,
      }),
    ).toBeNull();
    // Without the push history, All can't say it's every entry.
    expect(pill(/^All/)).toHaveTextContent("All9+");
  });

  it("lists reviews with their verdicts under a Reviews pill, each review's body once", async () => {
    const user = userEvent.setup();
    const body: PullRequestComment = {
      author: "ada",
      body: "Please split the migration out",
      created_at: SUBMITTED.at(1)!.submitted_at,
      url: PR_URL,
      review_id: "r-changes",
    };
    renderPage({ comments: [CONVERSATION, body], submittedReviews: SUBMITTED });
    expect(pill(/^Reviews/)).toHaveTextContent("Reviews3");
    await user.click(pill(/^Reviews/));
    expect(screen.getByRole("group", { name: /^ada approved these changes/ })).toBeVisible();
    expect(screen.getByRole("group", { name: /^ada requested changes/ })).toBeVisible();
    expect(screen.getByRole("group", { name: /^ben had a review dismissed/ })).toBeVisible();
    // The body shows under its verdict, and not again as a comment of its own.
    expect(screen.getAllByText("Please split the migration out")).toHaveLength(1);
    expect(screen.getAllByRole("article")).toHaveLength(1);
    await user.click(pill(/^Comments/));
    expect(screen.queryByText("Please split the migration out")).toBeNull();
  });

  it("leaves reviews' bodies as comments and the Reviews pill out where reviews weren't read, falling back to every kind", async () => {
    const user = userEvent.setup();
    const body: PullRequestComment = {
      author: "ada",
      body: "Please split the migration out",
      created_at: SUBMITTED.at(1)!.submitted_at,
      url: PR_URL,
      review_id: "r-changes",
    };
    const view = renderPage({ comments: [CONVERSATION, body], submittedReviews: SUBMITTED });
    await user.click(pill(/^Reviews/));

    // As in a shell that predates reviews, or the host's read.
    view.rerender(<PullRequestActivity {...view.props} submittedReviews={null} />);
    expect(
      within(screen.getByRole("navigation", { name: "Show" })).queryByRole("button", {
        name: /^Reviews/,
      }),
    ).toBeNull();
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^Comments/)).toHaveTextContent("Comments2");
    expect(screen.getByText("Please split the migration out")).toBeVisible();
    expect(screen.queryByRole("group", { name: /approved these changes/ })).toBeNull();

    view.rerender(<PullRequestActivity {...view.props} submittedReviews={SUBMITTED} />);
    expect(pill(/^Reviews/)).toHaveAttribute("aria-pressed", "true");
  });

  it("falls back to every kind while threads can't be read, and returns to Threads once they can", async () => {
    const user = userEvent.setup();
    const view = renderPage();
    await chooseThreads(user, /^Resolved/);
    expect(conversation()).toBeNull();

    // As when the session's machine sleeps: only the conversation can be read here now.
    view.rerender(<PullRequestActivity {...view.props} threadsAvailable={false} />);
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^All/)).toHaveTextContent("All1");
    expect(conversation()).toBeVisible();

    view.rerender(<PullRequestActivity {...view.props} threadsAvailable />);
    expect(pill(/^Threads/)).toHaveAttribute("aria-pressed", "true");
    expect(pill(/^Threads/)).toHaveTextContent("Threads · Resolved8");
    expect(threads()).toHaveLength(8);
  });

  it("falls back to every kind while pushes aren't read, and returns to Pushes once they are", async () => {
    const user = userEvent.setup();
    const view = renderPage({ events: [push("p1", 1)] });
    await user.click(pill(/^Pushes/));
    expect(conversation()).toBeNull();

    // As when another account's read brings no push history.
    view.rerender(<PullRequestActivity {...view.props} events={null} />);
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(conversation()).toBeVisible();

    view.rerender(<PullRequestActivity {...view.props} events={[push("p1", 1)]} />);
    expect(pill(/^Pushes/)).toHaveAttribute("aria-pressed", "true");
    expect(conversation()).toBeNull();
  });

  it("opens a conversation in Changes from its location or its comments' ⋯ menu", async () => {
    const user = userEvent.setup();
    const showInChanges = vi.fn();
    listOldestFirst();
    renderPage({ context: { ...CONTEXT, showInChanges } });
    const first = threads()[0]!;
    await user.click(
      within(first).getByRole("button", {
        name: "Show devtools/server/src/media/objects.rs, Line 273, in Changes",
      }),
    );
    expect(showInChanges).toHaveBeenCalledWith(expect.objectContaining({ id: "thread-0" }));

    await user.click(within(first).getByRole("button", { name: /Resolved conversation/ }));
    const comment = within(first).getByRole("article", { name: "Comment by reviewer-bot" });
    await user.click(within(comment).getByRole("button", { name: "Comment actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Show in Changes" }));
    expect(showInChanges).toHaveBeenCalledTimes(2);
    expect(showInChanges).toHaveBeenLastCalledWith(expect.objectContaining({ id: "thread-0" }));
  });

  it("leaves a conversation's location as text where nothing opens it in Changes", () => {
    renderPage();
    expect(within(threads()[0]!).queryByRole("button", { name: /in Changes$/ })).toBeNull();
  });

  it("searches from /, narrowing counts, and clears with Escape", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.keyboard("/");
    const search = screen.getByRole("searchbox", { name: "Search comments" });
    expect(search).toHaveFocus();
    await user.keyboard("tunnel loop (3)");
    expect(threads()).toHaveLength(1);
    expect(pill(/^All/)).toHaveTextContent("All1");
    await user.keyboard("{Escape}");
    expect(search).toHaveValue("");
    expect(threads()).toHaveLength(8);
  });

  it("leaves / to fields and to a hidden page", async () => {
    const user = userEvent.setup();
    const view = renderPage({ active: false });
    await user.keyboard("/");
    expect(screen.getByRole("searchbox", { name: "Search comments" })).not.toHaveFocus();
    view.rerender(<PullRequestActivity {...view.props} active />);
    const field = document.createElement("textarea");
    document.body.append(field);
    field.focus();
    await user.keyboard("/");
    expect(field).toHaveValue("/");
    field.remove();
  });

  describe("/ with two pull requests on screen", () => {
    const OTHER_URL = "https://github.com/acme/universe/pull/7";
    const searchIn = (container: HTMLElement) =>
      within(container).getByRole("searchbox", { name: "Search comments" });

    it("searches the Activity focus is in, or the one in the details focus is in", async () => {
      const user = userEvent.setup();
      const main = renderPage();
      const sidebar = renderPage({ pullRequestUrl: OTHER_URL });
      act(() => within(main.container).getByRole("button", { name: "Activity options" }).focus());
      await user.keyboard("/");
      expect(searchIn(main.container)).toHaveFocus();
      act(() =>
        within(sidebar.container).getByRole("button", { name: "Activity options" }).focus(),
      );
      await user.keyboard("/");
      expect(searchIn(sidebar.container)).toHaveFocus();
      cleanup();

      const props = main.props;
      render(
        <>
          <div data-pull-request-details="" data-testid="main">
            <button type="button">Main header</button>
            <PullRequestActivity {...props} />
          </div>
          <div data-pull-request-details="" data-testid="other">
            <PullRequestActivity {...props} pullRequestUrl={OTHER_URL} />
          </div>
        </>,
      );
      act(() => screen.getByRole("button", { name: "Main header" }).focus());
      await user.keyboard("/");
      expect(searchIn(screen.getByTestId("main"))).toHaveFocus();
    });

    it("searches the pane that owns its commands when focus is in neither page", async () => {
      const user = userEvent.setup();
      const props = renderPage().props;
      cleanup();
      const { container } = render(
        <>
          <button type="button">Pull request header</button>
          <div data-testid="main">
            <PaletteCommandScope enabled>
              <PullRequestActivity {...props} />
            </PaletteCommandScope>
          </div>
          <div data-testid="sidebar">
            <PaletteCommandScope enabled={false}>
              <PullRequestActivity {...props} pullRequestUrl={OTHER_URL} />
            </PaletteCommandScope>
          </div>
        </>,
      );
      act(() => within(container).getByRole("button", { name: "Pull request header" }).focus());
      await user.keyboard("/");
      expect(searchIn(screen.getByTestId("main"))).toHaveFocus();
    });

    it("searches a page in a sheet, but not from a menu open over it", async () => {
      const user = userEvent.setup();
      const props = renderPage().props;
      cleanup();
      render(
        <div role="dialog" aria-label="Pull request sidebar">
          <PullRequestActivity {...props} />
        </div>,
      );
      const search = screen.getByRole("searchbox", { name: "Search comments" });
      act(() => screen.getByRole("button", { name: "Activity options" }).focus());
      await user.keyboard("/");
      expect(search).toHaveFocus();
      const menu = document.createElement("div");
      menu.setAttribute("role", "menu");
      menu.tabIndex = -1;
      document.body.append(menu);
      act(() => menu.focus());
      await user.keyboard("/");
      expect(search).not.toHaveFocus();
      menu.remove();
    });
  });

  it("filters by file and author from the ⋯ menu, with a removable chip", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "Activity options" }));
    // File's submenu lists the files, the most-commented first, then by name.
    screen.getByRole("menuitem", { name: "File" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(await screen.findByRole("menuitemcheckbox", { name: /edge\.rs/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    // The menu stays open, so several values can be picked in a row.
    expect(screen.getByRole("menuitemcheckbox", { name: /edge\.rs/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.keyboard("{Escape}{Escape}");
    expect(threads()).toHaveLength(4);
    expect(conversation()).toBeNull();
    await user.click(screen.getByRole("button", { name: "Remove File filter" }));
    expect(threads()).toHaveLength(8);
    await chooseOption(user, "Author", /^example-ci-bot/);
    await user.keyboard("{Escape}{Escape}");
    expect(threads()).toHaveLength(0);
    expect(conversation()).toBeVisible();
  });

  it("filters pushes by author with comments and threads", async () => {
    const user = userEvent.setup();
    renderPage({ events: [push("p1", 1)] });
    await chooseOption(user, "Author", /^olaf/);
    await user.keyboard("{Escape}{Escape}");
    expect(screen.getByRole("group", { name: /^olaf pushed/ })).toBeVisible();
    expect(threads()).toHaveLength(0);
    expect(conversation()).toBeNull();
  });

  it("sorts newest first by default, and keeps Oldest First or By File once chosen", async () => {
    const user = userEvent.setup();
    const view = renderPage();
    const order = () =>
      screen
        .getAllByRole("listitem")
        .map((item) => item.querySelector("[aria-label]")?.getAttribute("aria-label"));
    expect(order().at(-1)).toBe("Comment by example-ci-bot");
    await chooseOption(user, "Sort", /^Oldest First/, "menuitemradio");
    expect(order()[0]).toBe("Comment by example-ci-bot");
    // The choice is this device's: a remounted Overview, or another PR, opens the same way.
    view.unmount();
    renderPage();
    expect(order()[0]).toBe("Comment by example-ci-bot");
    await chooseOption(user, "Sort", /^By File/, "menuitemradio");
    expect(order()[0]).toBe("Comment by example-ci-bot");
    expect(order()[1]).toMatch(/edge\.rs/);
  });

  it("switches order from ⌘K through the same choice as the Sort menu", async () => {
    renderPage();
    const sortCommand = command("Sort comments")!;
    expect(sortCommand.setting!.current).toBe("newest");
    await act(async () => {
      await sortCommand.setting!.choose("oldest");
    });
    expect(commentsOrderPreference.read()).toBe("oldest");
  });

  it("shows lower-bound counts and loads more conversations while more are on the host", async () => {
    const user = userEvent.setup();
    const more = reviews();
    more.page!.has_more = true;
    renderPage({ reviews: more });
    expect(pill(/^All/)).toHaveTextContent("All9+");
    expect(pill(/^Threads/)).toHaveTextContent("Threads8+");
    await user.click(screen.getByRole("button", { name: "Load more conversations" }));
    expect(more.loadMore).toHaveBeenCalled();

    await user.type(
      screen.getByRole("searchbox", { name: "Search comments" }),
      "nothing like this",
    );
    expect(screen.getByText("No matches in the loaded activity")).toBeVisible();
    expect(
      screen.getByText("Search and filters cover only the activity loaded so far."),
    ).toBeVisible();
  });

  it("offers a thread's later replies, counting what's loaded as a lower bound", async () => {
    const user = userEvent.setup();
    const paged = thread(0, { resolved: false });
    paged.comments.has_more = true;
    const context = { ...CONTEXT, loadComments: vi.fn() };
    renderPage({ reviews: reviews({}, [paged]), context });
    await user.click(screen.getByRole("button", { name: "Show more replies" }));
    expect(context.loadComments).toHaveBeenCalledWith(paged);
  });

  it("marks the conversation as partial when the host returned only some comments", () => {
    renderPage({ commentsPartial: true });
    expect(pill(/^All/)).toHaveTextContent("All9+");
    expect(pill(/^Comments/)).toHaveTextContent("Comments1+");
    expect(screen.getByRole("link", { name: "See them on GitHub" })).toBeVisible();
  });

  it("shows the conversation while threads load, without claiming zero, then explains a failed first read", async () => {
    const user = userEvent.setup();
    const view = renderPage({ reviews: reviews({ page: undefined }) });
    expect(pill(/^Threads/)).toHaveTextContent("Threads…");
    expect(pill(/^All/)).toHaveTextContent("All…");
    expect(conversation()).toBeVisible();
    expect(screen.queryByText("No activity yet")).toBeNull();

    const failed = reviews({ page: undefined, error: "GitHub is unavailable" });
    view.rerender(<PullRequestActivity {...view.props} reviews={failed} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t refresh review conversations: GitHub is unavailable",
    );
    await user.click(screen.getByRole("button", { name: "Try Again" }));
    expect(failed.retry).toHaveBeenCalled();
  });

  it("says loading, then unavailable, while nothing else is read", async () => {
    const view = renderPage({ comments: [], reviews: reviews({ page: undefined }) });
    expect(screen.getByText("Loading activity…")).toBeVisible();
    view.rerender(
      <PullRequestActivity
        {...view.props}
        reviews={reviews({ page: undefined, error: "GitHub is unavailable" })}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Activity Unavailable");
  });

  it("keeps loaded comments with a warning when a refresh fails, and offers a refresh when stale", async () => {
    const user = userEvent.setup();
    const failing = reviews({ error: "rate limited", historyStale: true });
    renderPage({ reviews: failing });
    expect(threads()).toHaveLength(8);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t refresh review conversations");
    expect(screen.getByText(/Comments changed on GitHub/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(failing.refreshHistory).toHaveBeenCalled();
  });

  it.each([
    ["threads", "Couldn’t load more conversations"],
    ["replies", "Couldn’t load more replies"],
  ] as const)("names a failed %s read and retries it", async (failure, title) => {
    const user = userEvent.setup();
    const failing = reviews({ error: "GitHub is unavailable", failure });
    renderPage({ reviews: failing });
    expect(threads()).toHaveLength(8);
    expect(screen.getByRole("alert")).toHaveTextContent(`${title}: GitHub is unavailable`);
    await user.click(screen.getByRole("button", { name: "Try Again" }));
    expect(failing.retry).toHaveBeenCalled();
  });

  it("shows the conversation and pushes where review threads can't be read", () => {
    renderPage({ threadsAvailable: false, events: [push("p1", 1)] });
    expect(
      within(screen.getByRole("navigation", { name: "Show" })).queryByRole("button", {
        name: /^Threads/,
      }),
    ).toBeNull();
    expect(screen.getByText(/Review conversations aren’t available here/)).toBeVisible();
    expect(conversation()).toBeVisible();
    expect(screen.getByRole("group", { name: /^olaf pushed/ })).toBeVisible();
    expect(threads()).toHaveLength(0);
  });

  it("hides a hidden comment, and keeps its text out of search until shown", async () => {
    const user = userEvent.setup();
    const hidden = thread(0, { resolved: false });
    hidden.comments.data[0] = {
      ...hidden.comments.data[0]!,
      body: "secret words",
      minimized: true,
    };
    renderPage({ reviews: reviews({}, [hidden]) });
    expect(screen.queryByText("secret words")).toBeNull();
    await user.type(screen.getByRole("searchbox", { name: "Search comments" }), "secret");
    expect(threads()).toHaveLength(0);
    await user.clear(screen.getByRole("searchbox", { name: "Search comments" }));
    await user.click(screen.getByRole("button", { name: "This comment was hidden. Show it" }));
    expect(screen.getByText("secret words")).toBeVisible();
    await user.type(screen.getByRole("searchbox", { name: "Search comments" }), "secret");
    expect(threads()).toHaveLength(1);
  });

  it("offers a reply only where the account may reply", () => {
    listOldestFirst();
    renderPage({
      reviews: reviews({}, [
        thread(0, { resolved: false }),
        thread(1, { resolved: false, can_reply: false }),
      ]),
    });
    const [canReply, cannotReply] = threads();
    expect(
      within(canReply!).getByRole("form", { name: "Reply to this conversation" }),
    ).toBeVisible();
    expect(within(cannotReply!).queryByRole("form")).toBeNull();
  });

  it("offers the Comments group's actions in ⌘K while shown, run by the same state as the toolbar", async () => {
    const more = reviews();
    more.page!.has_more = true;
    const view = renderPage({ reviews: more });
    expect(commands().map((candidate) => candidate.label)).toEqual(
      expect.arrayContaining([
        "Search comments",
        "Show comments",
        "Sort comments",
        "Filter comments by author",
        "Filter comments by file",
        "Filter comments by state",
        "Load more conversations",
      ]),
    );
    const showComments = command("Show comments")!;
    expect(showComments.setting!.choices.map((choice) => choice.label)).toEqual([
      "All (9+)",
      "Comments (1)",
      "Threads (8+)",
      "Pushes (0)",
      "Threads: Open (0+)",
      "Threads: Resolved (8+)",
    ]);
    await act(async () => {
      await showComments.setting!.choose("resolved");
    });
    expect(pill(/^Threads/)).toHaveAttribute("aria-pressed", "true");
    expect(command("Clear comment filters")).toBeDefined();
    act(() => command("Load more conversations")!.run());
    expect(more.loadMore).toHaveBeenCalled();
    await act(async () => {
      await command("Show comments")!.setting!.choose("comment");
    });
    expect(pill(/^Comments/)).toHaveAttribute("aria-pressed", "true");

    view.rerender(<PullRequestActivity {...view.props} active={false} />);
    expect(command("Search comments")).toBeUndefined();
  });

  it("says search covers only what's loaded while a thread has more replies, and loads them", async () => {
    const user = userEvent.setup();
    const paged = thread(0, { resolved: false });
    paged.comments.has_more = true;
    const context = { ...CONTEXT, loadComments: vi.fn() };
    // Every thread is loaded; only this one's later replies aren't.
    renderPage({ reviews: reviews({}, [paged, thread(1)]), context });
    expect(pill(/^All/)).toHaveTextContent("All3");

    await user.type(screen.getByRole("searchbox", { name: "Search comments" }), "a later reply");
    expect(screen.getByText("No matches in the loaded activity")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Load more replies (1 conversation)" }));
    expect(context.loadComments).toHaveBeenCalledWith(paged);
  });

  it("keeps an opened thread open while a new head reloads, and starts fresh on another pull request", async () => {
    const user = userEvent.setup();
    const view = renderPage();
    const fold = () => within(threads()[0]!).getByRole("button", { name: /Resolved conversation/ });
    await user.click(fold());
    expect(fold()).toHaveAttribute("aria-expanded", "true");
    await user.click(pill(/^Threads/));

    // A new head's read starts with no page: the entries go, then come back as they were.
    view.rerender(<PullRequestActivity {...view.props} reviews={reviews({ page: undefined })} />);
    expect(threads()).toHaveLength(0);
    view.rerender(<PullRequestActivity {...view.props} reviews={reviews()} />);
    expect(fold()).toHaveAttribute("aria-expanded", "true");
    expect(pill(/^Threads/)).toHaveAttribute("aria-pressed", "true");

    view.rerender(
      <PullRequestActivity
        {...view.props}
        reviews={reviews()}
        pullRequestUrl="https://github.com/acme/universe/pull/1"
      />,
    );
    expect(fold()).toHaveAttribute("aria-expanded", "false");
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
  });

  it("dates comments older than five weeks without 'ago'", () => {
    renderPage({ comments: [{ ...CONVERSATION, created_at: "2025-03-01T09:00:00Z" }] });
    const time = conversation()!.querySelector("time")!;
    expect(time.textContent).not.toMatch(/ago$/);
    expect(time.textContent).toMatch(/2025/);
  });

  it("names a file-level thread and a multi-line one", () => {
    listOldestFirst();
    renderPage({
      reviews: reviews({}, [
        thread(0, { resolved: false, subject_type: "FILE", diff_hunk: "" }),
        thread(1, { resolved: false, outdated: false, line: 20, start_line: 12 }),
      ]),
    });
    expect(within(threads()[0]!).getByText("File")).toBeVisible();
    expect(within(threads()[1]!).getByText("Lines 12–20")).toBeVisible();
  });
});

describe("revealing an entry", () => {
  const SCOPE = { conversationId: "conv_1", prUrl: PR_URL, account: "octocat" };
  const request = (target: ActivityRevealRequest["target"], id = 1): ActivityRevealRequest => ({
    id,
    scope: SCOPE,
    target,
  });
  const row = (key: string) =>
    document.querySelector<HTMLElement>(`[data-pull-request-activity="${CSS.escape(key)}"]`);

  it("shows a thread hidden by the kind, state, filters and search, unfolded and focused, and says what it cleared", async () => {
    const user = userEvent.setup();
    const view = renderPage({ events: [push("p1", 1)] });
    await user.click(pill(/^Pushes/));
    await chooseOption(user, "Author", /^olaf/);
    await user.keyboard("{Escape}{Escape}");
    await user.type(screen.getByRole("searchbox", { name: "Search comments" }), "Push");
    expect(threads()).toHaveLength(0);

    view.rerender(
      <PullRequestActivity
        {...view.props}
        revealRequest={request({ kind: "entry", key: "thread:thread-3" })}
      />,
    );
    expect(view.props.onRevealSettled).toHaveBeenCalledWith(1);
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("status", { name: "" })).toBeDefined();
    expect(screen.getByText("Cleared the author filter and the search to show it.")).toBeVisible();
    expect(row("thread:thread-3")).toHaveFocus();
    expect(row("thread:thread-3")).toHaveClass("animate-message-highlight");
    expect(within(row("thread:thread-3")!).getByText(/tunnel loop \(3\)/)).toBeVisible();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    // The note goes once the person changes what shows.
    await user.click(pill(/^Comments/));
    expect(screen.queryByText(/^Cleared /)).toBeNull();
  });

  it("widens the window to an earlier entry", () => {
    const many = reviews(
      {},
      Array.from({ length: 11 }, (_, i) => thread(i)),
    );
    const view = renderPage({ reviews: many });
    expect(conversation()).toBeNull();
    const key = document.querySelector("[data-pull-request-activity^='comment:']");
    expect(key).toBeNull();
    view.rerender(
      <PullRequestActivity
        {...view.props}
        revealRequest={request({ kind: "posted", key: postedCommentKey(CONVERSATION) })}
      />,
    );
    expect(conversation()).toBeVisible();
    expect(conversation()!.closest("li")).toHaveFocus();
    expect(screen.queryByRole("button", { name: /earlier$/ })).toBeNull();
  });

  it("waits for a posted comment's read before showing it", () => {
    const posted: PullRequestComment = {
      author: "octocat",
      body: "Thanks, fixed",
      created_at: "2026-10-22T09:00:00Z",
      url: `${PR_URL}#issuecomment-9`,
    };
    const view = renderPage();
    const pending = request({ kind: "posted", key: postedCommentKey(posted) });
    view.rerender(<PullRequestActivity {...view.props} revealRequest={pending} />);
    expect(view.props.onRevealSettled).not.toHaveBeenCalled();
    view.rerender(
      <PullRequestActivity
        {...view.props}
        comments={[CONVERSATION, posted]}
        revealRequest={pending}
      />,
    );
    expect(view.props.onRevealSettled).toHaveBeenCalledWith(1);
    expect(screen.getByRole("article", { name: "Comment by octocat" }).closest("li")).toHaveFocus();
  });

  it("never shows another reader's pending posted comment once the reader changes", async () => {
    const user = userEvent.setup();
    const base = renderPage({ comments: [], events: [push("p1", 1)] }).props;
    cleanup();
    let reveal: ReturnType<typeof useActivityRevealRequest>["reveal"] = () => {};
    function Panel({ account, loaded }: { account: string; loaded: boolean }) {
      const requests = useActivityRevealRequest({
        conversationId: CONTEXT.conversationId,
        prUrl: PR_URL,
        account,
      });
      reveal = requests.reveal;
      return (
        <PullRequestActivity
          {...base}
          context={{ ...CONTEXT, account }}
          comments={loaded ? [CONVERSATION] : []}
          revealRequest={requests.request}
          onRevealSettled={requests.settle}
        />
      );
    }
    const view = render(<Panel account="octocat" loaded={false} />);
    await user.click(pill(/^Pushes/));
    act(() => reveal({ kind: "posted", key: postedCommentKey(CONVERSATION) }));
    // The new reader's read lists the comment the old one posted; it isn't revealed for them.
    view.rerender(<Panel account="hubot" loaded />);
    expect(pill(/^Pushes/)).toHaveAttribute("aria-pressed", "true");
    expect(document.activeElement?.closest("[data-pull-request-activity]")).toBeNull();
  });

  it("shows the whole section with every kind for the Comments tab's old links", async () => {
    const user = userEvent.setup();
    const view = renderPage();
    await user.click(pill(/^Comments/));
    view.rerender(
      <PullRequestActivity {...view.props} revealRequest={request({ kind: "section" })} />,
    );
    expect(pill(/^All/)).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("region", { name: "Activity" })).toHaveFocus();
  });
});
