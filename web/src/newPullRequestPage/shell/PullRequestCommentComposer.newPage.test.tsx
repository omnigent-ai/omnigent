// Comment forms on the new pull request page write in the Markdown composer; the classic page
// keeps its plain textarea.

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { usePullRequestCommentComposer } from "@/newPullRequestPage/hooks/usePullRequests";
import { requestCommentBoxOpen } from "@/lib/commentBoxOpenRequests";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  type PullRequestAttachmentsAvailability,
  PullRequestAttachmentsContext,
} from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";

import { PullRequestCommentComposer, PullRequestCommentForm } from "./PullRequestCommentComposer";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";

interface PendingPost {
  body: Record<string, unknown>;
  respond: (response: Response) => void;
}

function stubFetch(): PendingPost[] {
  const posts: PendingPost[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { pathname } = new URL(String(input), "http://localhost");
      return new Promise<Response>((respond) => {
        if (pathname.endsWith("/github/comments") || pathname.endsWith("/replies"))
          posts.push({ body: JSON.parse(String(init?.body)), respond });
        else respond(new Response("{}", { status: 404 }));
      });
    }),
  );
  return posts;
}

function commentResponse(body: string): Response {
  return new Response(
    JSON.stringify({
      object: "session.github.pr_comment",
      comment: { author: "dev", body, created_at: "2026-11-07T10:00:00Z", url: `${PR_URL}#c1` },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  usePaletteCommandsStore.setState({ groups: {} });
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function wrapper({
  pageV2 = true,
  attachments = { available: true, needsHost: false },
}: { pageV2?: boolean; attachments?: PullRequestAttachmentsAvailability } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    const content = (
      <PullRequestAttachmentsContext.Provider value={attachments}>
        {children}
      </PullRequestAttachmentsContext.Provider>
    );
    return (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          {pageV2 ? (
            <PullRequestImageScope prUrl={PR_URL}>{content}</PullRequestImageScope>
          ) : (
            content
          )}
        </TooltipProvider>
      </QueryClientProvider>
    );
  };
}

function textboxNamed(name: string): HTMLTextAreaElement {
  // The new page's comment box starts folded to one line; open it the way a person would.
  const folded = screen.queryByRole("button", { name: "Add a comment…" });
  if (name === "New PR comment" && folded) fireEvent.click(folded);
  return screen.getByRole("textbox", { name }) as HTMLTextAreaElement;
}

/** ⌘↵ in the composer; jsdom isn't a Mac, so the chord is Control+Enter. */
function commandEnter(target: Element): void {
  act(() => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
}

describe("the comment form on the new pull request page", () => {
  it("writes Markdown with the toolbar and ⌘↵ posts it, then starts the next comment fresh", async () => {
    const posts = stubFetch();
    const Wrapper = wrapper();
    render(<PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account="@dev" />, {
      wrapper: Wrapper,
    });
    const textarea = textboxNamed("New PR comment");
    act(() => textarea.focus());
    fireEvent.change(textarea, { target: { value: "Looks good" } });
    textarea.setSelectionRange(0, "Looks good".length);
    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    expect(textarea.value).toBe("**Looks good**");
    commandEnter(textarea);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]!.body).toEqual({ pr_url: PR_URL, body: "**Looks good**" });
    await act(async () => posts[0]!.respond(commentResponse("**Looks good**")));
    // The posted draft cleared into a fresh, empty composer.
    await waitFor(() => expect(textboxNamed("New PR comment").value).toBe(""));
  });

  it("says how to attach where uploads work, and why not where they need the session's machine", () => {
    stubFetch();
    const { unmount } = render(
      <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />,
      { wrapper: wrapper() },
    );
    textboxNamed("New PR comment");
    expect(screen.getByText(/^Paste or drop images and videos to attach/)).toBeInTheDocument();
    expect(screen.getByTestId("markdown-composer-file-input")).toBeInTheDocument();
    unmount();
    render(<PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />, {
      wrapper: wrapper({ attachments: { available: false, needsHost: true } }),
    });
    textboxNamed("New PR comment");
    expect(screen.getByText(/^Attaching files needs the session’s machine/)).toBeInTheDocument();
    expect(screen.queryByTestId("markdown-composer-file-input")).toBeNull();
  });

  it("opens a folded reply into the composer, and Esc folds it again keeping the draft", async () => {
    stubFetch();
    function Reply() {
      const composer = usePullRequestCommentComposer("conv_1", PR_URL, "thread_1");
      return <PullRequestCommentForm {...composer} account="@dev" inline />;
    }
    render(<Reply />, { wrapper: wrapper() });
    fireEvent.focus(screen.getByRole("textbox", { name: "Reply to review conversation" }));
    const reply = textboxNamed("Reply to review conversation");
    expect(reply.tagName).toBe("TEXTAREA");
    expect(reply).toHaveFocus();
    fireEvent.change(reply, { target: { value: "Half a thought" } });
    fireEvent.keyDown(reply, { key: "Escape" });
    const folded = textboxNamed("Reply to review conversation");
    await waitFor(() => expect(folded.tagName).toBe("INPUT"));
    expect(folded.value).toBe("Half a thought");
  });

  describe("a new review conversation's box", () => {
    function NewConversation({ name, onCancel }: { name: string; onCancel: () => void }) {
      const composer = usePullRequestCommentComposer("conv_1", PR_URL, name);
      return (
        <PullRequestCommentForm {...composer} account="@dev" inline newThread onCancel={onCancel} />
      );
    }
    const paletteRows = () =>
      Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

    it("closes on Esc, as its Cancel does", () => {
      stubFetch();
      const onCancel = vi.fn();
      render(<NewConversation name="new_1" onCancel={onCancel} />, { wrapper: wrapper() });
      fireEvent.keyDown(textboxNamed("New review comment"), { key: "Escape" });
      expect(onCancel).toHaveBeenCalledOnce();
    });

    it("offers Cancel Comment in ⌘K for the box worked in last, and it closes that box", () => {
      stubFetch();
      const first = vi.fn();
      const second = vi.fn();
      render(
        <>
          <NewConversation name="new_1" onCancel={first} />
          <NewConversation name="new_2" onCancel={second} />
        </>,
        { wrapper: wrapper() },
      );
      const [firstBox, secondBox] = screen.getAllByRole("textbox", { name: "New review comment" });
      act(() => firstBox!.focus());
      act(() => secondBox!.focus());
      const cancels = paletteRows().filter((row) => row.label === "Cancel Comment");
      expect(cancels).toHaveLength(1);
      act(() => cancels[0]!.run());
      expect(second).toHaveBeenCalledOnce();
      expect(first).not.toHaveBeenCalled();
    });
  });

  it("keeps the classic page's textarea", () => {
    stubFetch();
    render(<PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />, {
      wrapper: wrapper({ pageV2: false }),
    });
    expect(textboxNamed("New PR comment").tagName).toBe("TEXTAREA");
    expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
  });
});

describe("the draft binding", () => {
  it("ignores an edit meant for a draft that was sent and cleared since", async () => {
    const posts = stubFetch();
    const { result } = renderHook(() => usePullRequestCommentComposer("conv_1", PR_URL), {
      wrapper: wrapper(),
    });
    act(() => result.current.setBody("first"));
    const sentGeneration = result.current.markdown.generation;
    act(() => result.current.post());
    await waitFor(() => expect(posts).toHaveLength(1));
    await act(async () => posts[0]!.respond(commentResponse("first")));
    await waitFor(() => expect(result.current.markdown.generation).toBe(sentGeneration + 1));
    act(() => {
      // A late upload from the sent comment.
      result.current.markdown.editDraft(
        () => ({ body: "late image", uploading: 0 }),
        sentGeneration,
      );
    });
    await act(async () => {});
    expect(result.current.draft.body).toBe("");
    const generationNow = result.current.markdown.generation;
    act(() => {
      result.current.markdown.editDraft(() => ({ body: "next" }), generationNow);
    });
    // React Query tells its subscribers a moment later.
    await waitFor(() => expect(result.current.draft.body).toBe("next"));
  });
});

describe("the folded comment box on the new page", () => {
  const folded = () => screen.getByRole("button", { name: "Add a comment…" });
  const paletteLabels = () =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .map((command) => command.label);

  function renderBox(props: { active?: boolean; account?: string | null } = {}) {
    const Wrapper = wrapper();
    const view = render(
      <PullRequestCommentComposer
        conversationId="conv_1"
        prUrl={PR_URL}
        account={props.account ?? "@dev"}
        active={props.active}
      />,
      { wrapper: Wrapper },
    );
    return { ...view, Wrapper };
  }

  it("starts as one row, and opening it puts the caret in the composer", () => {
    stubFetch();
    renderBox();
    expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();
    expect(folded()).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(folded());
    const textarea = textboxNamed("New PR comment");
    expect(textarea).toHaveFocus();
    expect(textarea).toHaveAttribute("placeholder", "Add to the conversation");
  });

  it("folds on Esc and on Cancel, keeping the draft and its first line, and focus returns", async () => {
    stubFetch();
    renderBox();
    const textarea = textboxNamed("New PR comment");
    fireEvent.change(textarea, { target: { value: "Half a thought\nand more" } });
    fireEvent.keyDown(textarea, { key: "Escape" });
    // The draft store tells the form a moment later; the row then shows the kept first line.
    const row = await screen.findByRole("button", { name: "Half a thought" });
    expect(row).toHaveFocus();
    fireEvent.click(row);
    const reopened = screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement;
    expect(reopened.value).toBe("Half a thought\nand more");
    // The caret goes to the end of the kept draft.
    expect(reopened).toHaveFocus();
    expect(reopened.selectionStart).toBe(reopened.value.length);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Half a thought" })).toHaveFocus();
  });

  it("can't fold while posting, folds after the post, and stays open when it fails", async () => {
    const posts = stubFetch();
    renderBox();
    const textarea = textboxNamed("New PR comment");
    fireEvent.change(textarea, { target: { value: "Ship it" } });
    commandEnter(textarea);
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    fireEvent.keyDown(textarea, { key: "Escape" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    fireEvent.keyDown(screen.getByRole("tabpanel", { name: "Preview" }), { key: "Escape" });
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeInTheDocument();
    await act(async () =>
      posts[0]!.respond(new Response(JSON.stringify({ detail: "Rejected" }), { status: 400 })),
    );
    expect(await screen.findByText(/Rejected/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Write" }));
    commandEnter(screen.getByRole("textbox", { name: "New PR comment" }));
    await waitFor(() => expect(posts).toHaveLength(2));
    await act(async () => posts[1]!.respond(commentResponse("Ship it")));
    await waitFor(() => expect(folded()).toBeInTheDocument());
  });

  it("opens by itself on a kept draft without taking focus", () => {
    stubFetch();
    const { unmount, Wrapper } = renderBox();
    fireEvent.change(textboxNamed("New PR comment"), { target: { value: "Kept" } });
    unmount();
    render(<button type="button">Elsewhere</button>);
    screen.getByRole("button", { name: "Elsewhere" }).focus();
    render(<PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />, {
      wrapper: Wrapper,
    });
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveValue("Kept");
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  });

  it("puts Cancel before Comment, and says who posts on both the row and the button", () => {
    render(
      <PullRequestCommentForm
        draft={{ body: "", pending: false, error: null }}
        setBody={() => {}}
        post={() => {}}
        account="@dev"
        markdown={{
          conversationId: "conv_1",
          prUrl: PR_URL,
          generation: 0,
          identity: "box",
          editDraft: () => {},
        }}
      />,
      { wrapper: wrapper() },
    );
    expect(folded()).toHaveAccessibleDescription("Comment as @dev");
    fireEvent.click(folded());
    const comment = screen.getByRole("button", { name: "Comment" });
    expect(comment).toHaveAccessibleDescription("Comment as @dev");
    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel.compareDocumentPosition(comment) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText(/^Comment as /, { selector: "p" })).toBeNull();
  });

  it("offers Add a Comment in ⌘K for the shown panel only, and Cancel Comment while open", () => {
    stubFetch();
    const { unmount } = renderBox();
    expect(paletteLabels()).toEqual(["Add a Comment"]);
    act(() => usePaletteCommandsStore.getState().groups && undefined);
    const add = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === "Add a Comment")!;
    act(() => add().run());
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveFocus();
    expect(paletteLabels()).toContain("Cancel Comment");
    const cancel = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Cancel Comment")!;
    act(() => cancel.run());
    expect(folded()).toBeInTheDocument();
    expect(paletteLabels()).toEqual(["Add a Comment"]);
    unmount();
    expect(paletteLabels()).toEqual([]);
  });

  it.each(["reply", "main box"] as const)(
    "offers one Cancel Comment with a reply open beside it, for the %s focused last",
    async (focusedLast) => {
      stubFetch();
      function Page() {
        const reply = usePullRequestCommentComposer("conv_1", PR_URL, "thread_1");
        return (
          <>
            <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account="@dev" />
            <PullRequestCommentForm {...reply} account="@dev" inline />
          </>
        );
      }
      render(<Page />, { wrapper: wrapper() });
      const main = textboxNamed("New PR comment");
      fireEvent.focus(screen.getByRole("textbox", { name: "Reply to review conversation" }));
      const reply = textboxNamed("Reply to review conversation");
      const [first, second] = focusedLast === "reply" ? [main, reply] : [reply, main];
      act(() => first.focus());
      act(() => second.focus());
      const cancels = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => command.label === "Cancel Comment");
      expect(cancels).toHaveLength(1);
      act(() => cancels[0]!.run());
      if (focusedLast === "reply") {
        // The reply folds to its one-line input; the main box stays open.
        await waitFor(() =>
          expect(
            screen.getByRole("textbox", { name: "Reply to review conversation" }).tagName,
          ).toBe("INPUT"),
        );
        expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeInTheDocument();
      } else {
        expect(folded()).toBeInTheDocument();
        expect(screen.getByRole("textbox", { name: "Reply to review conversation" }).tagName).toBe(
          "TEXTAREA",
        );
      }
    },
  );

  it("runs Add a Comment from Preview by going back to Write with the caret", async () => {
    stubFetch();
    renderBox();
    fireEvent.change(textboxNamed("New PR comment"), { target: { value: "Draft" } });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    const add = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Add a Comment")!;
    act(() => add.run());
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: "Write" })).toHaveAttribute("aria-selected", "true"),
    );
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveFocus(),
    );
  });

  it("withdraws Add a Comment and Cancel Comment while the page hides the box", () => {
    stubFetch();
    // The description editor hides the box with CSS; the box reads it as not displayed.
    // jsdom has neither API, so the test supplies both for this render.
    const prototype = HTMLElement.prototype as { checkVisibility?: () => boolean };
    const hadVisibility = "checkVisibility" in prototype;
    const original = prototype.checkVisibility;
    prototype.checkVisibility = () => false;
    const hadObserver = typeof ResizeObserver !== "undefined";
    if (!hadObserver)
      vi.stubGlobal(
        "ResizeObserver",
        class {
          observe() {}
          disconnect() {}
        },
      );
    onTestFinished(() => {
      if (hadVisibility) prototype.checkVisibility = original;
      else delete prototype.checkVisibility;
    });
    renderBox();
    fireEvent.click(folded());
    // Hidden by the page: neither of the box's rows is offered.
    expect(paletteLabels()).not.toContain("Add a Comment");
    expect(paletteLabels()).not.toContain("Cancel Comment");
  });

  it("registers nothing from a panel that isn't shown, so two panels never duplicate rows", () => {
    stubFetch();
    renderBox({ active: false });
    expect(paletteLabels()).toEqual([]);
    renderBox({ active: true });
    expect(paletteLabels()).toEqual(["Add a Comment"]);
  });
});

describe("Close and Reopen Pull Request in the box", () => {
  type Action = NonNullable<Parameters<typeof PullRequestCommentForm>[0]["stateAction"]>;
  function action(overrides: Partial<Action> = {}): Action {
    return {
      offer: { kind: "action", operation: "pull_request_close", label: "Close Pull Request" },
      running: false,
      runningOperation: null,
      notice: null,
      run: vi.fn(),
      retry: vi.fn(),
      checkAgain: vi.fn(async () => {}),
      checking: false,
      checkFailed: false,
      held: !!overrides.running,
      heldReason: overrides.running ? "Another change to this pull request is running." : null,
      dismiss: vi.fn(),
      ...overrides,
    };
  }
  const markdown = {
    conversationId: "conv_1",
    prUrl: PR_URL,
    generation: 0,
    identity: "box",
    editDraft: () => {},
  };
  function renderForm(stateAction: Action, body = "", pending = false) {
    return render(
      <PullRequestCommentForm
        draft={{ body, pending, error: null }}
        setBody={() => {}}
        post={() => {}}
        account="@dev"
        markdown={markdown}
        stateAction={stateAction}
        paletteActive
      />,
      { wrapper: wrapper() },
    );
  }
  const footerButtons = () =>
    Array.from(
      screen.getByRole("button", { name: "Cancel" }).parentElement!.querySelectorAll("button, a"),
    ).map((element) => element.textContent);
  const paletteRow = (label: string) =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((command) => command.label === label);

  it("sits between Cancel and Comment, and closes alone with an empty box", () => {
    const stateAction = action();
    renderForm(stateAction);
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    expect(footerButtons()).toEqual(["Cancel", "Close Pull Request", "Comment"]);
    fireEvent.click(screen.getByRole("button", { name: "Close Pull Request" }));
    expect(stateAction.run).toHaveBeenCalledExactlyOnceWith(false);
    // Closing alone is the page's own ⌘K row, on every tab; the box adds only its comment.
    expect(paletteRow("Close Pull Request")).toBeUndefined();
  });

  it("reads Close with Comment while the box has text, and posts it first", () => {
    const stateAction = action();
    renderForm(stateAction, "Superseded by #12");
    fireEvent.click(screen.getByRole("button", { name: "Close with Comment" }));
    expect(stateAction.run).toHaveBeenCalledExactlyOnceWith(true);
    expect(paletteRow("Close with Comment")).toBeDefined();
  });

  it("reads Reopen for a closed PR, and links to GitHub where this Mac can't change it", () => {
    const { unmount } = renderForm(
      action({
        offer: { kind: "action", operation: "pull_request_reopen", label: "Reopen Pull Request" },
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    expect(screen.getByRole("button", { name: "Reopen Pull Request" })).toBeInTheDocument();
    unmount();
    renderForm(action({ offer: { kind: "link", label: "Close on GitHub", url: PR_URL } }));
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    expect(screen.getByRole("link", { name: "Close on GitHub" })).toHaveAttribute("href", PR_URL);
    expect(paletteRow("Close Pull Request")).toBeUndefined();
  });

  it("shows nothing for a merged PR", () => {
    renderForm(action({ offer: { kind: "none" } }));
    fireEvent.click(screen.getByRole("button", { name: "Add a comment…" }));
    expect(footerButtons()).toEqual(["Cancel", "Comment"]);
  });

  it("holds the whole box while a close runs, from this box or another", () => {
    renderForm(action({ running: true }), "Why");
    expect(screen.getByRole("button", { name: "Closing…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Comment" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "New PR comment" }), { key: "Escape" });
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toBeInTheDocument();
    expect(paletteRow("Add a Comment")).toBeUndefined();
  });

  it("leaves a Close's notice and its ⌘K rows to the page, which shows them on every tab", () => {
    const refused = action({
      notice: {
        operation: "pull_request_close",
        attempt: "attempt-test",
        outcome: { kind: "refused", message: "No.", commentPosted: true },
        boundTo: {
          authGeneration: 0,
          actor: { login: "mac-octo", generation: 1 },
          host: { conversationId: "conv", key: "route" },
        },
      },
    });
    renderForm(refused);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try Again" })).toBeNull();
    expect(paletteRow("Try Closing Again")).toBeUndefined();
    expect(paletteRow("Dismiss Close Notice")).toBeUndefined();
  });

  it("holds Close while an earlier one's outcome is unknown, but leaves Comment alone", () => {
    renderForm(action({ held: true }), "Why");
    expect(screen.getByRole("button", { name: "Close with Comment" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Comment" })).toBeEnabled();
    expect(paletteRow("Close with Comment")).toBeUndefined();
  });

  it("runs the newest draft's action from ⌘K after the draft and action change", () => {
    const first = action();
    const second = action();
    const { rerender } = renderForm(first, "Why");
    rerender(
      <PullRequestCommentForm
        draft={{ body: "Why", pending: false, error: null }}
        setBody={() => {}}
        post={() => {}}
        account="@dev"
        markdown={{ ...markdown, identity: "next", generation: 1 }}
        stateAction={second}
        paletteActive
      />,
    );
    act(() => paletteRow("Close with Comment")!.run());
    expect(second.run).toHaveBeenCalledExactlyOnceWith(true);
    expect(first.run).not.toHaveBeenCalled();
  });
});

describe("Quote Reply's request to open the classic page's box", () => {
  const formFor = (identity: string) => ({
    draft: { body: "Kept", pending: false, error: null },
    setBody: () => {},
    post: () => {},
    account: "@dev",
    boxName: identity,
    markdown: {
      conversationId: "conv_1",
      prUrl: PR_URL,
      generation: 0,
      identity,
      editDraft: () => {},
    },
  });

  it("belongs to its draft: another owner's or PR's draft never takes the caret for it", () => {
    const Wrapper = wrapper({ pageV2: false });
    const view = render(
      <>
        <button type="button">Elsewhere</button>
        <PullRequestCommentForm {...formFor("a")} />
      </>,
      { wrapper: Wrapper },
    );
    act(() => {
      requestCommentBoxOpen("a");
    });
    const textarea = screen.getByRole("textbox", { name: "New PR comment" });
    expect(textarea).toHaveFocus();
    act(() => screen.getByRole("button", { name: "Elsewhere" }).focus());

    view.rerender(
      <>
        <button type="button">Elsewhere</button>
        <PullRequestCommentForm {...formFor("b")} />
      </>,
    );
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();

    // The new draft's own request still opens its box with the caret.
    act(() => {
      requestCommentBoxOpen("b");
    });
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveFocus();
  });
});
