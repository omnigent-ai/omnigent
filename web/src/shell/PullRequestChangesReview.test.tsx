import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { parsePatchFiles, type SelectedLineRange } from "@pierre/diffs";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { DiffReviewProps, DiffReviewSourceLayer } from "@/components/diff-review/DiffReview";
import type {
  PullRequestChangedFile,
  PullRequestCommentDraft,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { registerFindSurface } from "@/lib/findRouting";
import {
  PullRequestChangesReview,
  type PullRequestChangesReviewProps,
} from "./PullRequestChangesReview";
import { pullRequestFileActions } from "./pullRequestReview/pullRequestFileActions";
import type { PullRequestReviewAnnotation } from "./pullRequestReviewSource";
import { nextFileActionRequest, useFileActionStore } from "./fileCommands/fileActionStore";
import type { ReviewThreadContext } from "../newPullRequestPage/shell/PullRequestReviewThread";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readFileViewPreferences } from "@/lib/fileViewPreferences";
import { newReviewThreadDraftKey } from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

// The review renders on the real library in its own tests; here it records what
// the Changes tab gives it and renders the toolbar and status the tab adds.
const review = vi.hoisted(() => ({
  props: undefined as DiffReviewProps<PullRequestReviewAnnotation> | undefined,
}));
vi.mock("@/components/diff-review/DiffReview", () => ({
  DiffReview: (reviewProps: DiffReviewProps<PullRequestReviewAnnotation>) => {
    review.props = reviewProps;
    return (
      <div data-testid="review">
        <span data-testid="toolbar-status">{reviewProps.source?.toolbarStatus}</span>
        {reviewProps.source?.toolbar}
      </div>
    );
  },
}));

const PATH = "src/app.py";
// Two hunks: lines 1–3 and lines 20–22, each replacing its middle line.
const PATCH = [
  `diff --git a/${PATH} b/${PATH}`,
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -1,3 +1,3 @@",
  " first",
  "-old",
  "+new",
  " third",
  "@@ -20,3 +20,3 @@",
  " twenty",
  "-old",
  "+new",
  " twenty-two",
  "",
].join("\n");
const fileDiff = parsePatchFiles(PATCH)[0]!.files[0]!;

const context: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: "https://example.com/acme/app/pull/1",
  account: "octocat",
  canReply: true,
  canCreate: true,
  headSha: "head",
  baseSha: "base",
  loadComments: () => undefined,
  loadingComments: null,
};
const draftKey = newReviewThreadDraftKey(draftOwner(), context.conversationId, context.prUrl, PATH);

function thread(id: string, overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    path: PATH,
    side: "RIGHT",
    line: 2,
    start_line: null,
    subject_type: "LINE",
    resolved: false,
    outdated: false,
    can_reply: true,
    diff_hunk: "",
    comments: { data: [], has_more: false, next_cursor: null },
    ...overrides,
  };
}

const changedFile = {
  path: PATH,
  name: "app.py",
  bytes: 1,
  modified_at: null,
  status: "modified",
  lines_added: 2,
  lines_removed: 2,
} as PullRequestChangedFile;

function props(
  overrides: Partial<PullRequestChangesReviewProps> = {},
): PullRequestChangesReviewProps {
  return {
    files: [changedFile],
    filesByPath: new Map([[PATH, fileDiff]]),
    threadsByFile: new Map(),
    elsewhereThreads: [],
    context,
    matchingRevision: true,
    revision: "base:head",
    layout: "unified",
    onLayoutChange: () => undefined,
    focusedThread: null,
    unresolvedCount: 0,
    onNavigateUnresolved: () => undefined,
    loadingThreads: false,
    ...overrides,
  };
}

function renderReview(overrides: Partial<PullRequestChangesReviewProps> = {}) {
  const client = new QueryClient();
  const view = render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestChangesReview {...props(overrides)} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  const rerender = (next: Partial<PullRequestChangesReviewProps>) =>
    view.rerender(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <PullRequestChangesReview {...props({ ...overrides, ...next })} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
  return { client, rerender };
}

const source = (): DiffReviewSourceLayer<PullRequestReviewAnnotation> => review.props!.source!;

/** Renders a gutter "+" for the hovered line and clicks it. */
function clickGutter(lineNumber: number, side: SelectedLineRange["side"] = "additions") {
  const node = source().renderGutterUtility!(() => ({ lineNumber, side }), PATH);
  const { unmount } = render(<QueryProviderFor node={node} />);
  fireEvent.click(screen.getByRole("button", { name: "Start a new review conversation" }));
  unmount();
}

// The gutter control reads drafts from the review's query client.
let currentClient: QueryClient;
function QueryProviderFor({ node }: { node: ReactNode }) {
  return <QueryClientProvider client={currentClient}>{node}</QueryClientProvider>;
}

function select(range: SelectedLineRange | null, phase: "preview" | "commit") {
  act(() => source().onLineSelection!(PATH, range, phase));
}

const draft = (client: QueryClient) => client.getQueryData<PullRequestCommentDraft>(draftKey);

describe("PullRequestChangesReview", () => {
  it("starts a conversation on the hovered line from the gutter, then hides the gutter control", () => {
    const { client } = renderReview();
    currentClient = client;
    clickGutter(2);
    expect(draft(client)?.anchor).toMatchObject({ side: "RIGHT", line: 2, start_line: null });
    expect(source().annotations?.get(PATH)).toEqual([
      expect.objectContaining({
        side: "additions",
        lineNumber: 2,
        metadata: expect.objectContaining({ newThread: true }),
      }),
    ]);
    expect(source().renderGutterUtility!(() => ({ lineNumber: 2, side: "additions" }), PATH)).toBe(
      null,
    );
  });

  it("comments on the whole committed selection when the hovered line is inside it", () => {
    const { client } = renderReview();
    currentClient = client;
    select({ start: 1, end: 3, side: "deletions" }, "commit");
    clickGutter(2, "deletions");
    expect(draft(client)?.anchor).toMatchObject({ side: "LEFT", line: 3, start_line: 1 });
  });

  it("explains a selection that spans hunks above the file instead of starting a draft", () => {
    const { client } = renderReview();
    currentClient = client;
    select({ start: 2, end: 21, side: "additions" }, "commit");
    clickGutter(2);
    expect(draft(client)?.anchor).toBeUndefined();
    const fileRow = source().annotations?.get(PATH)?.[0];
    expect(fileRow).toMatchObject({
      lineNumber: 0,
      metadata: { fileLevel: true, selectionError: expect.stringMatching(/one diff hunk/) },
    });
    // A click on a single line clears the problem and starts the draft.
    select(null, "commit");
    clickGutter(2);
    expect(draft(client)?.anchor).toMatchObject({ side: "RIGHT", line: 2 });
    expect(
      source()
        .annotations?.get(PATH)
        ?.some((row) => row.metadata.selectionError),
    ).toBe(false);
  });

  it("shows a dragged selection until it's committed, and drops it when the pull request changes", () => {
    const { rerender } = renderReview();
    const dragged: SelectedLineRange = { start: 1, end: 2, side: "additions" };
    select(dragged, "preview");
    expect(source().selectedLines).toEqual({ path: PATH, range: dragged });
    select({ start: 1, end: 3, side: "additions" }, "commit");
    expect(source().selectedLines).toEqual({
      path: PATH,
      range: { start: 1, end: 3, side: "additions" },
    });
    rerender({ revision: "base:newer" });
    expect(source().selectedLines).toBeNull();
  });

  it("offers no gutter control when the reader can't comment, but still lets them pick lines", () => {
    renderReview({ context: { ...context, canCreate: false } });
    expect(source().renderGutterUtility).toBeUndefined();
    // Picked lines also feed the file's link on its code host.
    expect(source().onLineSelection).toBeDefined();
  });

  it("reveals the next unresolved conversation on its line and side", () => {
    const threadsByFile = new Map([
      [
        PATH,
        [thread("old-side", { side: "LEFT", line: 21 }), thread("outdated", { outdated: true })],
      ],
    ]);
    const { rerender } = renderReview({ threadsByFile });
    rerender({ focusedThread: { id: "old-side" } });
    expect(review.props!.revealTarget).toEqual({
      path: PATH,
      line: 21,
      side: "deletions",
      align: "center",
    });
    rerender({ focusedThread: { id: "outdated" } });
    expect(review.props!.revealTarget).toMatchObject({ path: PATH, line: 1, side: "additions" });
  });

  describe("a conversation asked for while the patch loads", () => {
    const threadsByFile = new Map([[PATH, [thread("old-side", { side: "LEFT", line: 21 })]]]);
    const loading = {
      threadsByFile,
      filesByPath: new Map(),
      patch: { status: "loading" } as const,
    };
    const ready = {
      threadsByFile,
      filesByPath: new Map([[PATH, fileDiff]]),
      patch: { status: "ready" } as const,
    };
    // The panel keeps one request object until the reader asks again.
    const request = { id: "old-side" };

    it("is revealed once its diff arrives, and only then", () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      expect(review.props!.revealTarget).toBeNull();
      rerender({ ...ready, focusedThread: request });
      expect(review.props!.revealTarget).toEqual({
        path: PATH,
        line: 21,
        side: "deletions",
        align: "center",
      });
    });

    it("isn't revealed again when the conversations refresh after it was", () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      rerender({ ...ready, focusedThread: request });
      const shown = review.props!.revealTarget;
      rerender({ ...ready, threadsByFile: new Map(threadsByFile), focusedThread: request });
      expect(review.props!.revealTarget).toBe(shown);
    });

    /** The conversation's row as the review draws it once its diff is in, scrolled and focused here. */
    const drawConversation = () => {
      const row = document.createElement("button");
      row.dataset.reviewThread = "old-side";
      row.getClientRects = () => [new DOMRect(0, 0, 10, 10)] as unknown as DOMRectList;
      row.scrollIntoView = vi.fn();
      act(() => {
        screen.getByTestId("review").append(row);
      });
      return row;
    };

    it("scrolls to and focuses the conversation once it's drawn", async () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      rerender({ ...ready, focusedThread: request });
      const row = drawConversation();
      await act(async () => {});
      expect(row.scrollIntoView).toHaveBeenCalledTimes(1);
      expect(document.activeElement).toBe(row);
    });

    it("neither scrolls to nor focuses it once the reader has done something else", async () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      fireEvent.wheel(screen.getByTestId("review"));
      rerender({ ...ready, focusedThread: request });
      const row = drawConversation();
      await act(async () => {});
      expect(row.scrollIntoView).not.toHaveBeenCalled();
      expect(document.activeElement).not.toBe(row);
    });

    it("neither scrolls to nor focuses it when it's drawn after the patch failed and was read again", async () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      const failed = { status: "failed", message: "No answer." } as const;
      rerender({ ...loading, patch: failed, focusedThread: request });
      rerender({ ...ready, focusedThread: request });
      const row = drawConversation();
      await act(async () => {});
      expect(row.scrollIntoView).not.toHaveBeenCalled();
      expect(review.props!.revealTarget).toBeNull();
    });

    it("still follows a newer request after one was given up", async () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      fireEvent.wheel(screen.getByTestId("review"));
      rerender({ ...ready, focusedThread: { id: "old-side" } });
      expect(review.props!.revealTarget).toMatchObject({ line: 21, side: "deletions" });
      const row = drawConversation();
      await act(async () => {});
      expect(row.scrollIntoView).toHaveBeenCalledTimes(1);
    });

    it("gives way to the reader acting before the diff arrives", () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      fireEvent.wheel(screen.getByTestId("review"));
      rerender({ ...ready, focusedThread: request });
      expect(review.props!.revealTarget).toBeNull();
    });

    it("gives up when the patch fails", () => {
      const { rerender } = renderReview(loading);
      rerender({ ...loading, focusedThread: request });
      rerender({
        ...loading,
        patch: { status: "failed", message: "No answer." },
        focusedThread: request,
      });
      rerender({ ...ready, focusedThread: request });
      expect(review.props!.revealTarget).toBeNull();
    });
  });

  it("loads unchanged lines ten at a time through the pull request's loader", () => {
    const loadDiffFiles = vi.fn();
    renderReview({ loadDiffFiles });
    expect(source().expandContext).toEqual({ load: loadDiffFiles, lineCount: 10 });
  });

  it("puts unresolved navigation and find in the review's toolbar", () => {
    const onNavigateUnresolved = vi.fn();
    renderReview({
      unresolvedCount: 2,
      onNavigateUnresolved,
      loadingThreads: true,
    });
    // Beside the line counts, not among the controls, so none of them moves as it comes and goes.
    const status = within(screen.getByTestId("toolbar-status")).getByRole("status");
    expect(status).toHaveTextContent("Loading review threads");
    expect(within(status).getByTestId("diff-review-toolbar-progress")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));
    fireEvent.click(screen.getByRole("button", { name: "Previous unresolved conversation" }));
    expect(onNavigateUnresolved.mock.calls).toEqual([[1], [-1]]);
    expect(review.props!.findOpen).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Find in changes" }));
    expect(review.props!.findOpen).toBe(true);
    act(() => review.props!.onFindClose());
    expect(review.props!.findOpen).toBe(false);
  });

  it("opens find from the platform's find chord while the reader works in the review", () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    renderReview();
    const outside = document.createElement("button");
    document.body.append(outside);
    const press = (init: KeyboardEventInit) =>
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "f", ...init });

    outside.focus();
    press({ metaKey: true });
    expect(review.props!.findOpen).toBe(false);

    const inside = screen.getByTestId("review");
    inside.tabIndex = -1;
    inside.focus();
    // Control+F moves the caret on macOS, so it stays with the focused control.
    press({ ctrlKey: true });
    expect(review.props!.findOpen).toBe(false);
    press({ metaKey: true });
    expect(review.props!.findOpen).toBe(true);
    outside.remove();
    vi.restoreAllMocks();
  });

  it("sends a second ⌘F from the review's find bar on to the app's find, and refocuses it from the diff", () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    const appFind = vi.fn();
    const release = registerFindSurface({
      fallback: true,
      isActive: () => true,
      openFind: appFind,
    });
    try {
      renderReview();
      const inside = screen.getByTestId("review");
      inside.tabIndex = -1;
      inside.focus();
      fireEvent.keyDown(inside, { key: "f", metaKey: true });
      expect(review.props!.findOpen).toBe(true);
      // Back in the diff with the bar shown, ⌘F refocuses the review's find.
      const focusRequest = review.props!.findFocusRequest;
      fireEvent.keyDown(inside, { key: "f", metaKey: true });
      expect(appFind).not.toHaveBeenCalled();
      expect(review.props!.findFocusRequest).not.toBe(focusRequest);
      // The review's find bar has the keyboard: ⌘F goes on to the app's find.
      const bar = document.createElement("div");
      bar.setAttribute("data-find-bar", "");
      bar.innerHTML = `<input aria-label="Find in changes" />`;
      inside.append(bar);
      const field = bar.querySelector("input")!;
      field.focus();
      fireEvent.keyDown(field, { key: "f", metaKey: true });
      expect(appFind).toHaveBeenCalledOnce();
      bar.remove();
    } finally {
      release();
      vi.restoreAllMocks();
    }
  });

  it("counts each file's conversations in its header, leaving the old path to the title", () => {
    const renamed = { ...fileDiff, prevName: "src/old.py" };
    renderReview({
      filesByPath: new Map([[PATH, renamed]]),
      threadsByFile: new Map([[PATH, [thread("one"), thread("two")]]]),
    });
    const extras = source().renderHeaderExtras!(review.props!.files[0]!);
    render(<div>{isValidElement(extras) ? (extras as ReactElement) : null}</div>);
    // The header's title already reads old → new, so the extras don't repeat it.
    expect(screen.queryByText(/src\/old\.py/)).toBeNull();
    expect(screen.getByTitle("2 conversations")).toHaveTextContent("2");
  });

  it("offers the file on the PR's host and copying its repository path, from the shared file actions", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderReview();
    const actions = review.props!.fileActions!(review.props!.files[0]!);
    expect(actions.map((action) => [action.id, action.label])).toEqual([
      ["open-on-code-host", "Open in GitHub"],
      ["copy-path", "Copy path"],
    ]);
    act(() => actions[0]!.run());
    expect(open).toHaveBeenCalledWith(
      `https://example.com/acme/app/blob/head/${PATH}`,
      "_blank",
      "noopener,noreferrer",
    );
    act(() => actions[1]!.run());
    expect(nextFileActionRequest(useFileActionStore.getState())).toMatchObject({
      command: "copy-path",
      target: { relativePath: PATH, absolutePath: null, hostId: null },
    });
    open.mockRestore();
  });

  it("opens files where the panel's file revision says: the pinned head, in the fork", () => {
    renderReview({
      fileRevision: { sha: "pinned", headRepositoryUrl: "https://example.com/fork/app" },
    });
    const [open] = review.props!.fileActions!(review.props!.files[0]!);
    expect(open).toMatchObject({ id: "open-on-code-host", disabledReason: undefined });
    expect(open!.link?.href).toBe(`https://example.com/fork/app/blob/pinned/${PATH}`);
  });

  it("can't open a file whose source repository the host says it doesn't know", () => {
    // An explicit null, as a new desktop app answers for a deleted fork.
    renderReview({ fileRevision: { sha: "pinned", headRepositoryUrl: null } });
    const [open] = review.props!.fileActions!(review.props!.files[0]!);
    expect(open).toMatchObject({
      id: "open-on-code-host",
      disabledReason: "The pull request's source repository isn't known.",
      link: undefined,
    });
    // An older host's answer says nothing, so the file opens in the PR's repository.
    renderReview({ fileRevision: { sha: "pinned" } });
    expect(review.props!.fileActions!(review.props!.files[0]!)[0]!.link?.href).toBe(
      `https://example.com/acme/app/blob/pinned/${PATH}`,
    );
  });

  it("offers a renamed file's previous path, from the list or the patch", () => {
    renderReview({ files: [{ ...changedFile, previous_path: "src/old.py" }] });
    const actions = review.props!.fileActions!(review.props!.files[0]!);
    expect(actions.map((action) => action.label)).toContain("Copy previous path");
    renderReview();
    expect(
      review.props!.fileActions!(review.props!.files[0]!).map((action) => action.label),
    ).not.toContain("Copy previous path");
  });

  it("paints its find under a pull request's highlight names", () => {
    renderReview();
    expect(review.props!.source?.findHighlightNames).toEqual({
      all: "pr-find",
      current: "pr-find-active",
    });
  });

  it("passes a file picked in the review's tree on to its tab", () => {
    const onFileChosen = vi.fn();
    renderReview({ onFileChosen });
    review.props!.onTreeFileChosen!("src/app.ts");
    expect(onFileChosen).toHaveBeenCalledWith("src/app.ts");
  });

  it("keeps each tree row's file menu where nothing opens files in new tabs", () => {
    renderReview();
    expect(review.props!.onOpenFileInNewTab).toBeUndefined();
    expect(review.props!.presentation?.treeRowActions).toBe(true);
    const labels = review.props!.fileActions!(review.props!.files[0]!).map(
      (action) => action.label,
    );
    expect(labels).toContain("Open in GitHub");
    expect(labels).not.toContain("Open in new tab");
  });

  it("names Example Host, and leaves a host no provider serves alone", () => {
    renderReview({
      context: {
        ...context,
        provider: "named-host",
        prUrl: "https://2.host.example.com/eng/universe/pull/9",
      },
    });
    const [exampleHost] = review.props!.fileActions!(review.props!.files[0]!);
    expect(exampleHost).toMatchObject({ id: "open-on-code-host", label: "Open in Example Host" });
    renderReview({ context: { ...context, provider: "gitlab" } });
    const ids = review.props!.fileActions!(review.props!.files[0]!).map((action) => action.id);
    expect(ids).toEqual(["copy-path"]);
  });

  it("can't open a deleted file on its host, and says why", () => {
    renderReview();
    const actions = pullRequestFileActions(
      { ...review.props!.files[0]!, change: "deleted" },
      { ...context, sha: context.headSha },
      null,
    );
    expect(actions.map(({ id, disabledReason, link }) => [id, disabledReason, link])).toEqual([
      ["open-on-code-host", "Deleted files can't be opened in GitHub.", undefined],
      ["copy-path", undefined, undefined],
    ]);
  });

  it("links the lines picked even where a new conversation can't be started", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderReview({ context: { ...context, canCreate: false } });
    // Only the "+" that starts a conversation needs that access.
    expect(review.props!.source?.renderGutterUtility).toBeUndefined();
    act(() =>
      review.props!.source!.onLineSelection!(
        PATH,
        { start: 4, end: 6, side: "additions" },
        "commit",
      ),
    );
    const [hostLink] = review.props!.fileActions!(review.props!.files[0]!);
    act(() => hostLink!.run());
    expect(open).toHaveBeenLastCalledWith(
      `https://example.com/acme/app/blob/head/${PATH}#L4-L6`,
      "_blank",
      "noopener,noreferrer",
    );
    open.mockRestore();
  });

  it("offers the review's commands only while the panel is showing", () => {
    renderReview();
    expect(review.props!.commandsActive).toBe(true);
    renderReview({ active: false });
    expect(review.props!.commandsActive).toBe(false);
  });

  it("hands the review the saved layout and wrapping, and saves a new wrap choice", () => {
    const onLayoutChange = vi.fn();
    renderReview({ layout: "split", onLayoutChange });
    expect(review.props!.layout).toBe("split");
    expect(review.props!.onLayoutChange).toBe(onLayoutChange);
    expect(review.props!.wrapLines).toBe(false);
    act(() => review.props!.onWrapLinesChange!(true));
    expect(review.props!.wrapLines).toBe(true);
    expect(readFileViewPreferences().wrapLines).toBe(true);
    // Pull requests come as patches, so whitespace can't be recomputed.
    expect(review.props!.onIgnoreWhitespaceChange).toBeUndefined();
  });
});

describe("while Changes shows the previous push", () => {
  const OUTDATED = {
    outdated: true,
    revision: "base:head",
    context: { ...context, canCreate: false, waitingForLatestPush: true },
  };
  const LATEST = {
    outdated: false,
    revision: "base-2:head-2",
    context: { ...context, headSha: "head-2", baseSha: "base-2" },
  };

  /** The draft's composer, rendered from the review's own annotation row. */
  function renderComposer(client: QueryClient) {
    const row = source()
      .annotations?.get(PATH)
      ?.find((annotation) => annotation.metadata.newThread);
    const node = source().renderAnnotation!(row!, PATH);
    const view = render(
      <QueryClientProvider client={client}>
        <TooltipProvider>{node}</TooltipProvider>
      </QueryClientProvider>,
    );
    const composer = within(view.getByRole("region", { name: "New review conversation" }));
    return { composer, unmount: view.unmount };
  }

  it("marks the diff Outdated and offers no gutter control to start a conversation", () => {
    renderReview(OUTDATED);
    expect(screen.getByText("Outdated")).toBeVisible();
    expect(screen.getByTestId("toolbar-status")).toHaveTextContent("Loading the latest push");
    expect(source().renderGutterUtility).toBeUndefined();
  });

  it("asks before Esc or Cancel throws away a new comment's text, and closes an empty one at once", () => {
    const { client } = renderReview();
    currentClient = client;
    clickGutter(2);
    act(() => {
      client.setQueryData<PullRequestCommentDraft>(draftKey, (current) => ({
        ...current!,
        body: "Worth keeping",
      }));
    });
    const { composer, unmount } = renderComposer(client);
    fireEvent.keyDown(composer.getByRole("textbox"), { key: "Escape" });
    const ask = screen.getByRole("dialog", { name: "Discard this comment?" });
    fireEvent.click(within(ask).getByRole("button", { name: "Keep Editing" }));
    expect(draft(client)?.body).toBe("Worth keeping");
    expect(composer.getByRole("textbox")).toHaveValue("Worth keeping");

    fireEvent.click(composer.getByRole("button", { name: "Cancel" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Discard this comment?" })).getByRole("button", {
        name: "Discard",
      }),
    );
    expect(draft(client)?.body).toBe("");
    unmount();

    clickGutter(2);
    const { composer: empty } = renderComposer(client);
    fireEvent.click(empty.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog", { name: "Discard this comment?" })).toBeNull();
    expect(draft(client)?.anchor ?? null).toBeNull();
  });

  it("keeps an open draft's text but lets it post only once the latest push loads", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      const { client, rerender } = renderReview();
      currentClient = client;
      clickGutter(2);
      act(() => {
        client.setQueryData<PullRequestCommentDraft>(draftKey, (current) => ({
          ...current!,
          body: "Kept across the push",
        }));
      });

      rerender(OUTDATED);
      const { composer: waiting, unmount } = renderComposer(client);
      expect(waiting.getByRole("status")).toHaveTextContent(
        "Your draft is saved. You can post it once the latest push loads.",
      );
      expect(waiting.getByRole("textbox")).toHaveValue("Kept across the push");
      expect(waiting.getByRole("button", { name: "Comment" })).toBeDisabled();
      fireEvent.click(waiting.getByRole("button", { name: "Comment" }));
      unmount();

      // After the swap the draft's old lines are stale; picked again, it posts on the latest push.
      rerender(LATEST);
      clickGutter(2);
      expect(draft(client)?.anchor).toMatchObject({ head_sha: "head-2", base_sha: "base-2" });
      const { composer: ready } = renderComposer(client);
      fireEvent.click(ready.getByRole("button", { name: "Comment" }));
      await waitFor(() => expect(writes(fetchSpy)).toHaveLength(1));
      expect(writes(fetchSpy)[0]).toMatchObject({
        body: "Kept across the push",
        anchor: { path: PATH, line: 2, head_sha: "head-2", base_sha: "base-2" },
      });
    } finally {
      fetchSpy.mockRestore();
    }
  });
});

/** The new conversations posted through the host, as their request bodies. */
function writes(fetchSpy: { mock: { calls: unknown[][] } }) {
  return fetchSpy.mock.calls
    .filter(([input]) => String(input).endsWith("/resources/github/review-threads"))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>);
}

describe("where the reader was in a PR's changes", () => {
  const ANCHOR = { path: PATH, line: 3, offset: 40 };
  const forPr = (prUrl: string, more: Partial<PullRequestChangesReviewProps> = {}) => ({
    context: { ...context, prUrl },
    ...more,
  });

  it("opens there again, and a PR of its own each", () => {
    const prUrl = "https://example.com/acme/app/pull/11";
    const first = render(
      <QueryClientProvider client={new QueryClient()}>
        <TooltipProvider>
          <PullRequestChangesReview {...props(forPr(prUrl))} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(review.props?.initialScrollAnchor).toBeUndefined();
    act(() => review.props!.onScrollAnchorChange!(ANCHOR));
    first.unmount();

    renderReview(forPr(prUrl));
    expect(review.props?.initialScrollAnchor).toEqual(ANCHOR);
    renderReview(forPr("https://example.com/acme/app/pull/12"));
    expect(review.props?.initialScrollAnchor).toBeUndefined();
  });

  it("gives way to a file the address names", () => {
    const prUrl = "https://example.com/acme/app/pull/13";
    const first = render(
      <QueryClientProvider client={new QueryClient()}>
        <TooltipProvider>
          <PullRequestChangesReview {...props(forPr(prUrl))} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    act(() => review.props!.onScrollAnchorChange!(ANCHOR));
    first.unmount();
    renderReview(forPr(prUrl, { revealPath: PATH }));
    expect(review.props?.initialScrollAnchor).toBeUndefined();
  });
});
