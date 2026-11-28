// A comment card's ⋯ menu, ⌘-click on its time and its ⌘K rows, with the real comment boxes
// Quote Reply writes into.

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { PullRequestComment } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";
import { PullRequestAttachmentsContext } from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestCommentComposer } from "@/newPullRequestPage/shell/PullRequestCommentComposer";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

const clipboard = vi.hoisted(() => ({ copyText: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => clipboard);
const toasts = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: toasts }));

import { useActivePullRequestComment } from "./activePullRequestComment";
import { ConversationEntry, ThreadEntry } from "./PullRequestCommentsEntries";
import { PullRequestCommentPaletteCommands } from "./PullRequestCommentActions";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const COMMENT_URL = `${PR_URL}#issuecomment-11`;

const COMMENT: PullRequestComment = {
  author: "grace",
  body: "Looks good\n\nShip it",
  created_at: "2026-11-07T10:00:00Z",
  url: COMMENT_URL,
};

const CONTEXT: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: PR_URL,
  account: "@dev",
  canReply: true,
  canComment: true,
  loadComments: vi.fn(),
  loadingComments: null,
};

const THREAD: ReviewThread = {
  id: "thread-1",
  path: "src/app.ts",
  side: "RIGHT",
  line: 4,
  start_line: null,
  subject_type: "LINE",
  resolved: false,
  outdated: false,
  can_reply: true,
  diff_hunk: "",
  comments: {
    data: [
      {
        id: "review-comment-1",
        author: "ada",
        body: "Rename this?",
        created_at: "2026-11-07T09:00:00Z",
        url: `${PR_URL}#discussion_r1`,
      },
      {
        id: "review-comment-2",
        author: "mallory",
        body: "Spam",
        created_at: "2026-11-07T09:05:00Z",
        url: `${PR_URL}#discussion_r2`,
        minimized: true,
      },
    ],
    has_more: false,
    next_cursor: null,
  },
};

const NO_ATTACHMENTS = { available: false, needsHost: false };

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestImageScope prUrl={PR_URL}>
          <PullRequestAttachmentsContext.Provider value={NO_ATTACHMENTS}>
            {children}
          </PullRequestAttachmentsContext.Provider>
        </PullRequestImageScope>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function Conversation({
  comment = COMMENT,
  context = CONTEXT,
  withBox = true,
  copies = 1,
  paletteActive = true,
}: {
  comment?: PullRequestComment;
  context?: ReviewThreadContext;
  withBox?: boolean;
  copies?: number;
  paletteActive?: boolean;
}) {
  return (
    <>
      <PullRequestCommentPaletteCommands
        conversationId={context.conversationId}
        prUrl={context.prUrl}
        active={paletteActive}
      />
      {Array.from({ length: copies }, (_, index) => (
        <div key={index} data-testid={`copy-${index}`}>
          <ConversationEntry
            comment={comment}
            host="github.com"
            pullRequestAuthor={null}
            pullRequestUrl={PR_URL}
            context={context}
          />
        </div>
      ))}
      {withBox && (
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account="@dev" />
      )}
    </>
  );
}

function openMenu(scope: HTMLElement | Document = document) {
  const trigger = within(scope as HTMLElement).getByRole("button", { name: "Comment actions" });
  trigger.focus();
  fireEvent.pointerDown(trigger, { button: 0 });
  return screen.getByRole("menu");
}

function choose(name: string, scope?: HTMLElement) {
  openMenu(scope);
  fireEvent.click(screen.getByRole("menuitem", { name }));
}

function paletteRows() {
  // The comment box offers rows under the same heading; these are the comment's own.
  return Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .filter((command) => command.id.startsWith("pull-request-comment:"));
}

beforeEach(() => {
  clipboard.copyText.mockClear();
  toasts.success.mockClear();
  toasts.error.mockClear();
});

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
  useActivePullRequestComment.setState({ active: null });
  vi.restoreAllMocks();
});

describe("a conversation comment's ⋯ menu", () => {
  it("offers Quote Reply, Copy Link and Open in Browser, without the old link icon", () => {
    render(<Conversation />, { wrapper: Wrapper });
    expect(screen.queryByRole("link", { name: /Open comment on/ })).toBeNull();
    const menu = openMenu();
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Quote Reply", "Copy Link", "Open in Browser"]);
  });

  it("copies the comment's link and says so", async () => {
    render(<Conversation />, { wrapper: Wrapper });
    choose("Copy Link");
    expect(clipboard.copyText).toHaveBeenCalledWith(COMMENT_URL);
    await waitFor(() => expect(toasts.success).toHaveBeenCalledWith("Link copied"));
  });

  it("says so when the link couldn't be copied", async () => {
    clipboard.copyText.mockImplementationOnce(() => Promise.reject(new Error("denied")));
    render(<Conversation />, { wrapper: Wrapper });
    choose("Copy Link");
    await waitFor(() => expect(toasts.error).toHaveBeenCalledWith("Couldn’t copy the link"));
  });

  it("opens the comment in the browser", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<Conversation />, { wrapper: Wrapper });
    choose("Open in Browser");
    expect(open).toHaveBeenCalledWith(COMMENT_URL, "_blank", "noopener,noreferrer");
  });

  it("offers no ⋯ for a review's body with no box to quote into, which has no link of its own", () => {
    render(
      <Conversation
        comment={{ ...COMMENT, url: PR_URL }}
        withBox={false}
        context={{ ...CONTEXT, canComment: false }}
      />,
      {
        wrapper: Wrapper,
      },
    );
    expect(screen.queryByRole("button", { name: "Comment actions" })).toBeNull();
  });

  it("offers only the link actions where the pull request takes no comments", () => {
    render(<Conversation withBox={false} context={{ ...CONTEXT, canComment: false }} />, {
      wrapper: Wrapper,
    });
    const menu = openMenu();
    expect(within(menu).queryByRole("menuitem", { name: "Quote Reply" })).toBeNull();
  });
});

describe("the comment's time", () => {
  // jsdom isn't a Mac, so the open-on-GitHub click is Ctrl-click there.
  it("opens the comment on GitHub on a Ctrl-click, and does nothing on a plain one", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<Conversation />, { wrapper: Wrapper });
    const time = document.querySelector("time")!;
    fireEvent.click(time);
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(time, { metaKey: true });
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(time, { ctrlKey: true });
    expect(open).toHaveBeenCalledWith(COMMENT_URL, "_blank", "noopener,noreferrer");
  });

  it("does nothing for a comment with no link of its own", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<Conversation comment={{ ...COMMENT, url: PR_URL }} />, { wrapper: Wrapper });
    fireEvent.click(document.querySelector("time")!, { ctrlKey: true });
    expect(open).not.toHaveBeenCalled();
  });
});

describe("Quote Reply into the conversation's comment box", () => {
  it("opens the folded box with the quote after the draft, caret at the end", async () => {
    render(<Conversation />, { wrapper: Wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    const box = screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "I agree." } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull(),
    );

    choose("Quote Reply");
    const reopened = (await screen.findByRole("textbox", {
      name: "New PR comment",
    })) as HTMLTextAreaElement;
    expect(reopened.value).toBe("I agree.\n\n> Looks good\n>\n> Ship it\n\n");
    await waitFor(() => expect(reopened).toHaveFocus());
    expect(reopened.selectionStart).toBe(reopened.value.length);
  });

  it("leaves Preview for Write, so the caret can go after the quote", async () => {
    render(<Conversation />, { wrapper: Wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    fireEvent.change(screen.getByRole("textbox", { name: "New PR comment" }), {
      target: { value: "Draft" },
    });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    expect(screen.getByRole("tab", { name: "Preview" })).toHaveAttribute("aria-selected", "true");

    choose("Quote Reply");
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Write" })).toHaveAttribute("aria-selected", "true"),
    );
    const box = screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement;
    expect(box.value).toBe("Draft\n\n> Looks good\n>\n> Ship it\n\n");
    await waitFor(() => expect(box).toHaveFocus());
    expect(box.selectionStart).toBe(box.value.length);
  });
});

describe("Quote Reply into the classic page's comment box", () => {
  function ClassicWrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return (
      <QueryClientProvider client={client}>
        <TooltipProvider>{children}</TooltipProvider>
      </QueryClientProvider>
    );
  }

  it("goes back to Write and puts the caret after the quote", async () => {
    render(<Conversation />, { wrapper: ClassicWrapper });
    fireEvent.change(screen.getByRole("textbox", { name: "New PR comment" }), {
      target: { value: "Draft" },
    });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();

    choose("Quote Reply");
    const box = (await screen.findByRole("textbox", {
      name: "New PR comment",
    })) as HTMLTextAreaElement;
    expect(box.value).toBe("Draft\n\n> Looks good\n>\n> Ship it\n\n");
    await waitFor(() => expect(box).toHaveFocus());
    expect(box.selectionStart).toBe(box.value.length);
  });
});

describe("Quote Reply in a review conversation", () => {
  function Thread({ context = CONTEXT, thread = THREAD }) {
    return (
      <>
        <PullRequestCommentPaletteCommands
          conversationId={context.conversationId}
          prUrl={context.prUrl}
          active
        />
        <ThreadEntry
          thread={thread}
          context={context}
          host="github.com"
          pullRequestAuthor={null}
          pullRequestUrl={PR_URL}
          expanded
          onExpandedChange={vi.fn()}
          revealed={new Set(["review-comment-2"])}
          onReveal={vi.fn()}
        />
      </>
    );
  }

  it("quotes into the thread's own reply box", async () => {
    render(<Thread />, { wrapper: Wrapper });
    const [first] = screen.getAllByRole("article");
    choose("Quote Reply", first);
    // The folded one-line reply opens into the full composer, with the quote and the caret.
    await waitFor(() =>
      expect(
        (
          screen.getByRole("textbox", {
            name: "Reply to review conversation",
          }) as HTMLTextAreaElement
        ).value,
      ).toBe("> Rename this?\n\n"),
    );
    const reply = screen.getByRole("textbox", { name: "Reply to review conversation" });
    expect(reply.tagName).toBe("TEXTAREA");
    await waitFor(() => expect(reply).toHaveFocus());
  });

  it("never quotes a hidden comment, even one shown on request", () => {
    render(<Thread />, { wrapper: Wrapper });
    const hidden = screen.getAllByRole("article")[1];
    const menu = openMenu(hidden);
    expect(within(menu).queryByRole("menuitem", { name: "Quote Reply" })).toBeNull();
    expect(within(menu).getByRole("menuitem", { name: "Copy Link" })).toBeInTheDocument();
  });

  it("offers no Quote Reply where the thread takes no replies", () => {
    render(<Thread thread={{ ...THREAD, can_reply: false }} />, { wrapper: Wrapper });
    const menu = openMenu(screen.getAllByRole("article")[0]);
    expect(within(menu).queryByRole("menuitem", { name: "Quote Reply" })).toBeNull();
  });
});

describe("⌘K rows for the focused comment", () => {
  it("offers the rows of the comment last focused, and runs them on it", () => {
    render(<Conversation />, { wrapper: Wrapper });
    expect(paletteRows()).toEqual([]);
    act(() => screen.getByRole("article").focus?.());
    fireEvent.focus(screen.getByRole("button", { name: "Comment actions" }));
    expect(paletteRows().map((row) => row.label)).toEqual([
      "Quote Reply",
      "Copy Comment Link",
      "Open Comment in Browser",
    ]);
    expect(paletteRows()[0].description).toBe("Comment by grace");
    act(() =>
      paletteRows()
        .find((row) => row.label === "Copy Comment Link")!
        .run(),
    );
    expect(clipboard.copyText).toHaveBeenCalledWith(COMMENT_URL);
  });

  it("keeps the rows once the menu opened, after focus moves to ⌘K", () => {
    render(<Conversation />, { wrapper: Wrapper });
    openMenu();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    act(() => (document.activeElement as HTMLElement | null)?.blur());
    expect(paletteRows()).toHaveLength(3);
  });

  it("shows only one comment's rows when the same comment shows twice", () => {
    render(<Conversation copies={2} />, { wrapper: Wrapper });
    fireEvent.focus(
      within(screen.getByTestId("copy-0")).getByRole("button", { name: "Comment actions" }),
    );
    fireEvent.focus(
      within(screen.getByTestId("copy-1")).getByRole("button", { name: "Comment actions" }),
    );
    expect(paletteRows()).toHaveLength(3);
  });

  it("offers none while the panel isn't the one shown", () => {
    render(<Conversation paletteActive={false} />, { wrapper: Wrapper });
    fireEvent.focus(screen.getByRole("button", { name: "Comment actions" }));
    expect(paletteRows()).toEqual([]);
  });

  it("withdraws them when the comment leaves the page", () => {
    const view = render(<Conversation />, { wrapper: Wrapper });
    fireEvent.focus(screen.getByRole("button", { name: "Comment actions" }));
    expect(paletteRows()).toHaveLength(3);
    view.rerender(<Conversation copies={0} />);
    expect(paletteRows()).toEqual([]);
  });

  it("withdraws them when the panel moves to another pull request", () => {
    const view = render(<Conversation />, { wrapper: Wrapper });
    fireEvent.focus(screen.getByRole("button", { name: "Comment actions" }));
    view.rerender(
      <Conversation context={{ ...CONTEXT, prUrl: "https://github.com/acme/app/pull/8" }} />,
    );
    expect(paletteRows()).toEqual([]);
  });

  it("shows Quote Reply unavailable, with why, while the comment posts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    render(<Conversation />, { wrapper: Wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    const box = screen.getByRole("textbox", { name: "New PR comment" });
    fireEvent.change(box, { target: { value: "Posting this" } });
    // ⌘↵ posts; jsdom isn't a Mac, so the chord is Control+Enter.
    act(() => {
      box.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
    });
    await screen.findByRole("button", { name: "Posting…" });
    fireEvent.focus(screen.getByRole("button", { name: "Comment actions" }));
    const quote = paletteRows().find((row) => row.label === "Quote Reply")!;
    expect(quote.availability).toEqual({
      available: false,
      reason: "Wait for the comment to post",
    });
    act(() => quote.run());
    expect(
      (screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement).value,
    ).toBe("Posting this");
    vi.unstubAllGlobals();
  });
});
