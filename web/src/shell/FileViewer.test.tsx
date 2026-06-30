// Tests for FileViewer's comments-panel open/close semantics and URL sync:
//
//   1. Panel stays closed on fresh open regardless of whether the file has comments.
//   2. Panel stays closed on fresh open when the file has no comments.
//   3. Arrow navigation preserves panel-open state (open → file with no comments).
//   4. Arrow navigation preserves panel-closed state (closed → file with comments).
//   5. Late query resolution does NOT override a user's manual toggle (race condition).
//   6. ?diff=1 URL param initializes diff view on open.
//   7. Toggling diff on writes ?diff=1 to URL.
//   8. Toggling diff off removes ?diff from URL.
//   9. ?comment=<id> URL param applies linked comment and clears the param.
//  10. ?comment= with unknown ID leaves panel closed and param intact.
//  11. Copy-link button is present in the header.
//  12. Comments are marked seen (inbox-clearing registry) only while the
//      comments panel is open — never from merely opening the file.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";

// ── Mock heavy child components ───────────────────────────────────────────────

vi.mock("./CodeViewer", () => ({
  CodeViewer: () => <div data-testid="code-viewer" />,
}));

vi.mock("./CommentsPanel", () => ({
  // Render a sentinel so tests can assert panel visibility without
  // pulling in CommentsPanel's full dependency tree.
  // Expose each comment as a button so tests can trigger onClickComment.
  CommentsPanel: ({
    onClickComment,
    comments,
  }: {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    onClickComment?: (comment: any) => void;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    comments?: any[];
  }) => (
    <div data-testid="comments-panel">
      {comments?.map((c: { id: string }) => (
        <button
          key={c.id}
          type="button"
          aria-label={`comment ${c.id}`}
          onClick={() => onClickComment?.(c)}
        />
      ))}
    </div>
  ),
}));

vi.mock("./MonacoDiffViewer", () => ({
  MonacoDiffViewer: () => <div data-testid="diff-viewer" />,
}));

// ── Mock hooks ────────────────────────────────────────────────────────────────

vi.mock("@/hooks/useComments", () => ({
  useComments: vi.fn(),
  useAddComment: vi.fn(() => ({ mutate: vi.fn() })),
  useUpdateComment: vi.fn(() => ({ mutate: vi.fn() })),
  useDeleteComment: vi.fn(() => ({ mutate: vi.fn() })),
}));

vi.mock("@/hooks/useFileContent", () => ({
  useFileContent: vi.fn(() => ({ data: { content: "", path: "file1.py" } })),
}));

vi.mock("@/hooks/useFileDiff", () => ({
  // Diff payload present (the diff view only renders once data has loaded).
  useFileDiff: vi.fn(() => ({ data: { before: "old", after: "new" } })),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: vi.fn(() => ({
    data: {
      available: true,
      data: [
        { path: "file1.py", bytes: 10, modified_at: null, name: "file1.py", status: "modified" },
      ],
    },
  })),
}));

vi.mock("@/hooks/useResizablePanel", () => ({
  useResizablePanel: vi.fn(() => ({
    panelWidth: 400,
    handleProps: {
      onMouseDown: vi.fn(),
      onKeyDown: vi.fn(),
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      tabIndex: 0,
    },
    isDesktop: true,
  })),
}));

vi.mock("@/hooks/CommentSenderContext", () => ({
  CommentSenderProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useOptionalCommentSender: vi.fn(() => null),
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn((selector: (s: { boundAgentId: null; status: string }) => unknown) =>
    selector({ boundAgentId: null, status: "idle" }),
  ),
}));

// ── Test helpers ──────────────────────────────────────────────────────────────

import { useComments } from "@/hooks/useComments";
import { useFileDiff } from "@/hooks/useFileDiff";
import { getSeenCommentIds } from "@/hooks/useSeenComments";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { classifyAndRemapComments, FileViewer } from "./FileViewer";
import type { ChangedSort } from "./FlatFileList";

const useCommentsMock = vi.mocked(useComments);

function makeCommentsQuery(data: Comment[] | undefined) {
  return { data } as ReturnType<typeof useComments>;
}

function makeComment(id: string): Comment {
  return {
    id,
    conversation_id: "conv_1",
    path: "file1.py",
    start_index: 0,
    end_index: 5,
    body: "test comment",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: "hello",
    created_by: null,
  };
}

/**
 * Renders the current URL search params into a testid element so URL-sync
 * tests can assert on param changes without reaching into router internals.
 */
function LocationDisplay() {
  const [params] = useSearchParams();
  return <div data-testid="url-params">{params.toString()}</div>;
}

interface RenderProps {
  open?: boolean;
  path?: string;
  /**
   * Initial URL search string (without leading "?"), e.g. "diff=1" or
   * "comment=c1". Defaults to empty (no URL params).
   */
  initialSearch?: string;
  /** Spy for the close affordance; defaults to a throwaway mock. */
  onClose?: () => void;
  /** Sort order for the prev/next navigation; defaults to the component default. */
  sort?: ChangedSort;
  /** Enables the prev/next nav header when provided. */
  onNavigateTo?: (path: string) => void;
}

/**
 * Build the full JSX tree for a render or rerender call.
 *
 * FileViewer calls useSearchParams, so it must live inside a Router.
 * MemoryRouter lets us seed the URL (including search params) without a
 * real browser environment. A LocationDisplay sibling lets tests read
 * the current params after state changes.
 */
function viewerTree({
  open = false,
  path = "file1.py",
  initialSearch = "",
  onClose = vi.fn(),
  sort,
  onNavigateTo,
}: RenderProps = {}) {
  const url = initialSearch ? `/?${initialSearch}` : "/";
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <LocationDisplay />
        <FileViewer
          open={open}
          conversationId="conv_1"
          path={path}
          onClose={onClose}
          sort={sort}
          onNavigateTo={onNavigateTo}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function renderViewer(props: RenderProps = {}) {
  return render(viewerTree(props));
}

// ── Setup / teardown ──────────────────────────────────────────────────────────

beforeEach(() => {
  useCommentsMock.mockReset();
  // FileViewer persists global view preferences (diff/layout/preview) to
  // localStorage. Clear it between tests so a preference written by one test
  // can't leak into another that asserts the hardcoded defaults.
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  // Restore any getBoundingClientRect spy installed by the width-gating tests.
  vi.restoreAllMocks();
});

/**
 * Drive FileViewer's content-area width measurement: define a ResizeObserver
 * (jsdom has none, so the measure effect would otherwise no-op) and make every
 * element's getBoundingClientRect report `width`. FileViewer calls measure()
 * synchronously in the effect, so the width lands on first render.
 */
function installContentWidth(width: number): void {
  class StubResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width,
    height: 0,
    top: 0,
    left: 0,
    right: width,
    bottom: 0,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("FileViewer comments panel open/close semantics", () => {
  it("keeps the panel closed on fresh open even when the file has comments", () => {
    // The panel no longer auto-opens — users open it manually via the icon.
    useCommentsMock.mockReturnValue(makeCommentsQuery([makeComment("c1")]));
    const { rerender } = renderViewer({ open: false });

    expect(screen.queryByTestId("comments-panel")).toBeNull();

    // Transition to open — panel should auto-open because data has comments.
    rerender(viewerTree({ open: true, path: "file1.py" }));

    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });

  it("leaves the panel closed on fresh open when the file has no comments", () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    const { rerender } = renderViewer({ open: false });

    rerender(viewerTree({ open: true, path: "file1.py" }));

    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });

  it("preserves panel-open state when navigating to a file with no comments", () => {
    // Open the viewer and manually open the comments panel.
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    const { rerender } = renderViewer({ open: true, path: "file1.py" });

    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();

    // Arrow navigation: same `open=true`, different path, no comments.
    // The initialized flag must stay set so the user's manual open choice is preserved.
    rerender(viewerTree({ open: true, path: "file2.py" }));

    // Panel should remain open — the user hasn't toggled it.
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();
  });

  it("preserves panel-closed state when navigating to a file with comments", () => {
    // First open: panel stays closed (no manual toggle).
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    const { rerender } = renderViewer({ open: false });

    rerender(viewerTree({ open: true, path: "file1.py" }));
    expect(screen.queryByTestId("comments-panel")).toBeNull();

    // Navigate to a file with comments — panel should stay closed.
    useCommentsMock.mockReturnValue(makeCommentsQuery([makeComment("c2")]));
    rerender(viewerTree({ open: true, path: "file2.py" }));

    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });

  it("does not override a manual user toggle when query data arrives late", () => {
    // Viewer opens while data is still loading.
    useCommentsMock.mockReturnValue(makeCommentsQuery(undefined));
    const { rerender } = renderViewer({ open: false });

    rerender(viewerTree({ open: true, path: "file1.py" }));
    // Panel is closed (data not yet available).
    expect(screen.queryByTestId("comments-panel")).toBeNull();

    // User manually opens the panel before data arrives.
    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();

    // Data arrives with no comments — must not close the manually-opened panel.
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    rerender(viewerTree({ open: true, path: "file1.py" }));

    // User's manual open must be preserved.
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();
  });
});

describe("FileViewer comment seen marking", () => {
  it("does not mark comments seen while the panel is closed", () => {
    // The inbox-clearing contract: merely opening a file (markers in
    // the gutter, panel collapsed) must NOT count as reading its
    // comments — the user reported exactly this over-clearing. If
    // this fails with a populated registry, the mark-seen effect
    // lost its commentsOpen gate.
    useCommentsMock.mockReturnValue(makeCommentsQuery([makeComment("c1")]));
    renderViewer({ open: true, path: "file1.py" });

    expect(screen.queryByTestId("comments-panel")).toBeNull();
    expect(getSeenCommentIds().has("c1")).toBe(false);
  });

  it("marks comments seen when the panel is opened", () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([makeComment("c1")]));
    renderViewer({ open: true, path: "file1.py" });
    expect(getSeenCommentIds().has("c1")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));

    // Panel visible ⇒ the comment bodies are on screen ⇒ seen.
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();
    expect(getSeenCommentIds().has("c1")).toBe(true);
  });

  it("marks the linked comment seen when ?comment= auto-opens the panel", () => {
    // The inbox "Open file" deep link relies on this: the linked
    // comment opens the panel, which is what records it as seen and
    // clears the inbox item.
    useCommentsMock.mockReturnValue(makeCommentsQuery([makeComment("c1")]));
    renderViewer({ open: true, path: "file1.py", initialSearch: "comment=c1" });

    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();
    expect(getSeenCommentIds().has("c1")).toBe(true);
  });
});

describe("FileViewer prev/next navigation order", () => {
  // Three changed files whose alphabetical order (a, b, c) differs from their
  // recency order (b newest → c → a oldest). Viewing b.py, the "X/N" index must
  // follow whichever sort the Changes list is using, or it won't match the list
  // position the user clicked from.
  const changedFiles = [
    { path: "a.py", bytes: 1, modified_at: 100, name: "a.py", status: "modified" as const },
    { path: "b.py", bytes: 1, modified_at: 300, name: "b.py", status: "modified" as const },
    { path: "c.py", bytes: 1, modified_at: 200, name: "c.py", status: "modified" as const },
  ];

  // alpha: [a, b, c] → b is 2nd → "2/3". recent: [b, c, a] → b is 1st → "1/3".
  it.each([
    { sort: "alpha" as const, expected: "2/3" },
    { sort: "recent" as const, expected: "1/3" },
  ])("renders the index per the $sort sort prop", ({ sort, expected }) => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: { available: true, data: changedFiles },
    } as ReturnType<typeof useWorkspaceChangedFiles>);
    try {
      renderViewer({ open: true, path: "b.py", sort, onNavigateTo: vi.fn() });

      // The index span sits between the prev/next buttons. Its text proves the
      // navigation list was sorted by `sort`; before the fix it was hard-coded
      // alphabetical, so "recent" would wrongly show "2/3" here.
      const prev = screen.getByRole("button", { name: "Previous file" });
      const indexSpan = prev.parentElement?.querySelector("span.tabular-nums");
      expect(indexSpan?.textContent).toBe(expected);
    } finally {
      // Restore the default single-file mock for later tests.
      vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
        data: {
          available: true,
          data: [
            {
              path: "file1.py",
              bytes: 10,
              modified_at: null,
              name: "file1.py",
              status: "modified",
            },
          ],
        },
      } as ReturnType<typeof useWorkspaceChangedFiles>);
    }
  });
});

describe("FileViewer URL sync — diff param", () => {
  it("initializes diff view when URL contains ?diff=1 and the file is in the changed list", async () => {
    // file1.py is returned by the useWorkspaceChangedFiles mock → isDiffAvailable=true.
    // Starting with ?diff=1 means diffActive is initialized to true, so viewMode="diff".
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    renderViewer({ open: true, path: "file1.py", initialSearch: "diff=1" });

    // The diff view must render (not CodeViewer) when diff is active.
    // Failure: diffActive was not initialized from the URL param (remained false).
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("code-viewer")).toBeNull();
  });

  it("writes ?diff=1 to the URL when the diff toggle button is clicked", async () => {
    // Start with no URL params and diff inactive.
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    renderViewer({ open: true, path: "file1.py" });

    // Baseline: CodeViewer shown, no diff param in URL.
    expect(screen.getByTestId("code-viewer")).toBeInTheDocument();
    expect(screen.getByTestId("url-params").textContent).not.toContain("diff=");

    fireEvent.click(screen.getByRole("button", { name: "Show diff" }));

    // After toggle: diff view shown, ?diff=1 added to URL.
    // Failure: diff sync useEffect did not call setSearchParams after diffActive changed.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
    expect(screen.getByTestId("url-params").textContent).toContain("diff=1");
  });

  it("removes ?diff from the URL when diff is toggled off", async () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    renderViewer({ open: true, path: "file1.py", initialSearch: "diff=1" });

    // Baseline: diff view active.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Exit diff view" }));

    // After toggle off: CodeViewer shown, ?diff param removed.
    // Failure: setSearchParams was not called to delete the diff param.
    expect(screen.queryByTestId("diff-viewer")).toBeNull();
    expect(screen.getByTestId("url-params").textContent).not.toContain("diff=");
  });

  it("shows a loading state until the diff payload loads (no collapsed-null mount)", () => {
    // While the diff query is in flight `data` is undefined. We must NOT mount
    // Monaco yet: useFileDiff uses null for new/deleted files, so collapsing the
    // loading state into null would mount with the wrong content and mis-set EOL
    // (onMount runs once). Failure here = the diff mounts before data arrives.
    vi.mocked(useFileDiff).mockReturnValue({ data: undefined } as ReturnType<typeof useFileDiff>);
    try {
      useCommentsMock.mockReturnValue(makeCommentsQuery([]));
      renderViewer({ open: true, path: "file1.py", initialSearch: "diff=1" });
      expect(screen.queryByTestId("diff-viewer")).toBeNull();
      expect(screen.getByText("Loading diff…")).toBeInTheDocument();
    } finally {
      // Restore the default (payload present) so later tests render the diff.
      vi.mocked(useFileDiff).mockReturnValue({
        data: { before: "old", after: "new" },
      } as ReturnType<typeof useFileDiff>);
    }
  });

  it("surfaces the server's reason instead of hanging on the loading state when the diff fetch fails", () => {
    // On error, useFileDiff's `data` stays undefined — which would otherwise
    // read as still-loading forever. The diff view must show the failure
    // reason (e.g. a git_status_failed 500) so the read error is visible.
    vi.mocked(useFileDiff).mockReturnValue({
      data: undefined,
      isError: true,
      error: new Error("git status timed out after 5.0s"),
    } as ReturnType<typeof useFileDiff>);
    try {
      useCommentsMock.mockReturnValue(makeCommentsQuery([]));
      renderViewer({ open: true, path: "file1.py", initialSearch: "diff=1" });
      expect(screen.queryByTestId("diff-viewer")).toBeNull();
      expect(screen.queryByText("Loading diff…")).toBeNull();
      expect(
        screen.getByText(/Failed to load:\s*git status timed out after 5\.0s/),
      ).toBeInTheDocument();
    } finally {
      // Restore the default (payload present) so later tests render the diff.
      vi.mocked(useFileDiff).mockReturnValue({
        data: { before: "old", after: "new" },
      } as ReturnType<typeof useFileDiff>);
    }
  });
});

describe("FileViewer URL sync — comment param", () => {
  it("opens the comments panel when ?comment= matches a loaded comment", () => {
    const comment = makeComment("c1");
    useCommentsMock.mockReturnValue(makeCommentsQuery([comment]));

    renderViewer({ open: true, path: "file1.py", initialSearch: "comment=c1" });

    // Panel must open because the linked comment was found and applied.
    // Failure: linkedCommentAppliedRef logic did not run, or commentsQuery.data
    // was not available when the effect fired.
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();
  });

  it("applies the linked comment only once per component lifecycle", () => {
    // The one-shot ref (linkedCommentAppliedRef) prevents the effect from
    // re-applying the comment when comment data refreshes mid-session
    // (e.g. a polling refetch). The ?comment= param is intentionally kept in
    // the URL while the viewer is open; it's cleared by AppShell on close or
    // when the user navigates to a different file.
    const comment = makeComment("c1");
    useCommentsMock.mockReturnValue(makeCommentsQuery([comment]));
    const { rerender } = renderViewer({
      open: true,
      path: "file1.py",
      initialSearch: "comment=c1",
    });

    // First apply: panel opens.
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();

    // User manually closes the panel.
    fireEvent.click(screen.getByRole("button", { name: "Hide comments" }));
    expect(screen.queryByTestId("comments-panel")).toBeNull();

    // Comment data refreshes — same comment ID still in deps, effect would
    // re-apply if not for the one-shot ref.
    useCommentsMock.mockReturnValue(makeCommentsQuery([comment, makeComment("c2")]));
    rerender(viewerTree({ open: true, path: "file1.py", initialSearch: "comment=c1" }));

    // Panel must stay closed — linkedCommentAppliedRef.current=true prevents
    // the effect from applying the comment a second time.
    // Failure: the ref guard was removed, causing the panel to reopen on data refresh.
    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });

  it("leaves the panel closed when ?comment= ID is not found in the loaded data", () => {
    // If the linked comment no longer exists (e.g. was deleted), the panel must
    // not open — the guard `if (!comment) return` prevents it.
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));

    renderViewer({ open: true, path: "file1.py", initialSearch: "comment=nonexistent" });

    // Panel must stay closed — no comment was matched.
    // Failure: the guard was removed, causing the panel to open even when the
    // comment could not be found.
    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });
});

describe("FileViewer URL sync — comment param (write)", () => {
  it("adds ?comment=<id> to the URL when a comment is clicked in the panel", () => {
    // Clicking a comment in CommentsPanel should sync its ID into the URL so
    // the address bar is always shareable without needing the explicit Copy Link button.
    // Failure: onClickComment handler doesn't call setSearchParams, so the param
    // is never written and the URL has no ?comment= after the click.
    const comment = makeComment("c1");
    useCommentsMock.mockReturnValue(makeCommentsQuery([comment]));

    renderViewer({ open: true, path: "file1.py" });

    // Manually open the panel.
    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();

    // Click the comment via the mock's exposed button.
    fireEvent.click(screen.getByRole("button", { name: "comment c1" }));

    // ?comment=c1 must now appear in the URL.
    // Failure: setSearchParams was not called from onClickComment.
    expect(screen.getByTestId("url-params").textContent).toContain("comment=c1");
  });

  it("updates ?comment= in the URL when a different comment is clicked", () => {
    // Clicking a second comment should replace the existing ?comment= param,
    // not accumulate multiple IDs.
    // Failure: setSearchParams was not called, or the param was appended
    // instead of replaced, leaving both IDs in the URL.
    const c1 = makeComment("c1");
    const c2 = makeComment("c2");
    useCommentsMock.mockReturnValue(makeCommentsQuery([c1, c2]));

    renderViewer({ open: true, path: "file1.py" });

    // Manually open the panel.
    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));

    fireEvent.click(screen.getByRole("button", { name: "comment c1" }));
    expect(screen.getByTestId("url-params").textContent).toContain("comment=c1");

    fireEvent.click(screen.getByRole("button", { name: "comment c2" }));

    // URL must reflect the NEW selection, not accumulate both.
    expect(screen.getByTestId("url-params").textContent).toContain("comment=c2");
    expect(screen.getByTestId("url-params").textContent).not.toContain("comment=c1");
  });
});

describe("FileViewer copy-link button", () => {
  it("renders a Copy link button in the viewer header toolbar", () => {
    // The button must always be present when the viewer is open so users can
    // share a link to the current file (with its current diff/view state baked in).
    // Failure: the button was not added to the toolbar, or its aria-label changed.
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    renderViewer({ open: true });

    expect(screen.getByRole("button", { name: "Copy link to file" })).toBeInTheDocument();
  });
});

function makeAnchoredComment(
  overrides: Partial<Comment> &
    Pick<Comment, "id" | "start_index" | "end_index" | "anchor_content">,
): Comment {
  return {
    conversation_id: "conv_1",
    path: "file1.py",
    body: "comment body",
    status: "draft",
    created_at: 0,
    created_by: null,
    ...overrides,
  } as Comment;
}

describe("classifyAndRemapComments", () => {
  it("buckets addressed comments separately and never remaps them", () => {
    const fileContent = "AAAA\nhello world\n";
    const addressed = makeAnchoredComment({
      id: "c_done",
      status: "addressed",
      start_index: 0,
      end_index: 5,
      anchor_content: "hello",
    });

    const result = classifyAndRemapComments([addressed], fileContent);

    expect(result.open).toHaveLength(0);
    expect(result.addressed).toHaveLength(1);
    expect(result.addressed[0].start_index).toBe(0);
    expect(result.addressed[0].end_index).toBe(5);
  });

  it("keeps a draft comment at its stored offsets when the anchor still matches", () => {
    const c = makeAnchoredComment({
      id: "c1",
      start_index: 0,
      end_index: 5,
      anchor_content: "hello",
    });

    const result = classifyAndRemapComments([c], "hello world");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(0);
    expect(result.open[0].end_index).toBe(5);
  });

  it("remaps a draft comment's offsets when an edit above the anchor shifts it", () => {
    const anchor = "target text";
    const originalStart = 12;
    const fileContent = "NEW HEADER\npadding line\n" + anchor + " trailing";
    const newStart = fileContent.indexOf(anchor);
    expect(newStart).not.toBe(originalStart); // the anchor really moved

    const c = makeAnchoredComment({
      id: "c2",
      start_index: originalStart,
      end_index: originalStart + anchor.length,
      anchor_content: anchor,
    });

    const result = classifyAndRemapComments([c], fileContent);

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(newStart);
    expect(result.open[0].end_index).toBe(newStart + anchor.length);
  });

  it("keeps a draft comment (detached) when its anchor was deleted from the file", () => {
    const c = makeAnchoredComment({
      id: "c3",
      start_index: 40,
      end_index: 60,
      anchor_content: "def deleted_function():",
    });

    // Anchor text is absent — comment must be kept at stored offsets, not dropped.
    const result = classifyAndRemapComments([c], "completely different content");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].id).toBe("c3");
    expect(result.open[0].start_index).toBe(40);
    expect(result.open[0].end_index).toBe(60);
  });

  it("falls back to a global search when no occurrence is near the stored offset", () => {
    // Only occurrence is ~600 chars from the stale stored offset (outside the
    // ±200 nearby window), so the global-search fallback must still find it.
    const anchor = "x = 1";
    const fileContent = "y = 2\n".repeat(100) + anchor;
    const realIdx = fileContent.indexOf(anchor);

    const c = makeAnchoredComment({
      id: "c4",
      start_index: 0,
      end_index: anchor.length,
      anchor_content: anchor,
    });

    const result = classifyAndRemapComments([c], fileContent);

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(realIdx);
    expect(result.open[0].end_index).toBe(realIdx + anchor.length);
  });

  it("keeps draft comments at stored offsets while file content is still loading", () => {
    // Empty fileContent (query unresolved) must not drop comments.
    const c = makeAnchoredComment({
      id: "c5",
      start_index: 10,
      end_index: 20,
      anchor_content: "something",
    });

    const result = classifyAndRemapComments([c], "");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].start_index).toBe(10);
    expect(result.open[0].end_index).toBe(20);
  });

  it("keeps an anchor-less draft comment unchanged", () => {
    const c = makeAnchoredComment({
      id: "c6",
      start_index: 0,
      end_index: 0,
      anchor_content: null,
    });

    const result = classifyAndRemapComments([c], "any content");

    expect(result.open).toHaveLength(1);
    expect(result.open[0].id).toBe("c6");
  });

  // Spec/xfail: a draft comment whose anchor can no longer be found
  // is currently returned in `open` at its stale stored offsets with no marker,
  // so the UI shows it as if still attached. The desired behavior is a
  // first-class detached/stale flag the UI can surface. `it.fails` asserts the
  // current code does NOT yet flag detached comments; flip to a normal `it`
  // once that lands.
  it.fails("flags a draft comment as detached when its anchor was deleted", () => {
    const c = makeAnchoredComment({
      id: "c_detached",
      start_index: 40,
      end_index: 60,
      anchor_content: "def deleted_function():",
    });

    const result = classifyAndRemapComments([c], "completely different content");

    // Spec: the un-remappable comment carries an explicit detached/stale marker.
    const flagged = result.open[0] as Comment & { detached?: boolean; stale?: boolean };
    expect(flagged.detached ?? flagged.stale).toBe(true);
  });
});

// ── Header close affordance ─────────────────────────────────────────────────
//
// The left-side ← back arrow is the dismiss affordance for the mobile
// full-screen overlay (non-frameless). On desktop the viewer is embedded in
// the tabbed Files rail (frameless), where tabs own open/close, so the back
// button is hidden there.

describe("FileViewer header close affordance", () => {
  it("invokes onClose when the back/close button is clicked (mobile)", () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    const onClose = vi.fn();
    // viewerTree renders without `frameless`, i.e. the mobile overlay mode
    // that keeps the back button.
    render(viewerTree({ open: true, onClose }));

    fireEvent.click(screen.getByRole("button", { name: "Close file viewer" }));

    // Clicking the back arrow dismisses the mobile overlay. A failure here
    // means the back button was removed from the mobile path too (it should
    // only be gated out in frameless/desktop mode).
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("hides the back button in frameless (desktop rail) mode", () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <FileViewer frameless open conversationId="conv_1" path="file1.py" onClose={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // No back button in the embedded tabbed editor — the absence proves the
    // [aria-label="Close file viewer"] mode toggle is gone on desktop. A
    // failure here means the gating regressed and the button reappeared.
    expect(screen.queryByRole("button", { name: "Close file viewer" })).toBeNull();
  });
});

// The comments panel defaults closed on each mount; toggling it open renders
// the panel. (Cross-remount persistence was removed with the single Files tab.)
describe("FileViewer comments panel", () => {
  it("defaults closed and renders when the user opens it", () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    render(viewerTree({ open: true }));

    expect(screen.queryByTestId("comments-panel")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show comments" }));
    expect(screen.getByTestId("comments-panel")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide comments" }));
    expect(screen.queryByTestId("comments-panel")).toBeNull();
  });
});

// ── localStorage persistence (survive a page refresh) ───────────────────────
//
// The diff/layout/preview choices are global preferences persisted to
// localStorage so they survive a full page reload — modeled as an unmount +
// a brand-new mount with NO URL params (the state a refresh starts from).
// commentsOpen is deliberately not persisted.

describe("FileViewer view-preference persistence across refresh", () => {
  it("a fresh mount restores the diff + split layout chosen by a previous instance", async () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));

    // First instance: user turns diff on and switches to split. The viewer's
    // persist effect writes both to localStorage.
    const first = render(viewerTree({ open: true }));
    fireEvent.click(screen.getByRole("button", { name: "Show diff" }));
    fireEvent.click(screen.getByRole("button", { name: "Split view" }));

    // Simulate a page refresh: tear the tree down and mount a brand-new viewer
    // with no URL params. The only way it can come up in split-diff is by
    // reading the persisted preference.
    first.unmount();
    render(viewerTree({ open: true }));

    // The split diff actually renders: the diff viewer is present and the
    // toggle offers "Unified view" (its label is the *other* layout). If the
    // write or seed-read were broken, diff would be off and the viewer absent.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Unified view" })).toBeInTheDocument();
  });

  it("?diff=1 forces diff on even when the persisted preference is diff-off", async () => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));

    // First instance: turn diff on then back off so a diff-off preference is
    // written to storage.
    const first = render(viewerTree({ open: true }));
    fireEvent.click(screen.getByRole("button", { name: "Show diff" }));
    fireEvent.click(screen.getByRole("button", { name: "Exit diff view" }));
    first.unmount();

    // A shared link with ?diff=1 must still open in diff view, overriding the
    // persisted diff-off preference.
    render(viewerTree({ open: true, initialSearch: "diff=1" }));

    // The diff viewer is present — diffActive=true came from ?diff=1, not
    // storage (which holds false). If the URL override were dropped the viewer
    // would be absent.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
  });
});

// ── Split/unified toggle width gating ───────────────────────────────────────
//
// Side-by-side ("split") is only usable once the diff area clears Monaco's
// 900px breakpoint; below that the toggle is hidden so users aren't offered a
// no-op control. file1.py is a changed file, so diff is available.

describe("FileViewer split-toggle width gating", () => {
  it("shows the split/unified toggle when the diff area is at least 900px wide", async () => {
    installContentWidth(1000);
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    // Seed diff-active via localStorage (as if the user turned it on previously).
    render(viewerTree({ open: true, initialSearch: "diff=1" }));

    // Diff is showing and the measured width (1000) clears the 900px threshold,
    // so the layout toggle is offered.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Split view" })).toBeInTheDocument();
  });

  it("hides the split/unified toggle when the diff area is narrower than 900px", async () => {
    installContentWidth(600);
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    render(viewerTree({ open: true, initialSearch: "diff=1" }));

    // Diff is still shown — only the layout toggle is gated. At 600px (< 900)
    // split would be forced inline by Monaco, so the toggle is suppressed.
    expect(await screen.findByTestId("diff-viewer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Split view" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Unified view" })).toBeNull();
  });
});

describe("FileViewer keyboard shortcut — Alt+← / Alt+→", () => {
  const multipleFiles = [
    { path: "a.py", bytes: 1, modified_at: 100, name: "a.py", status: "modified" as const },
    { path: "b.py", bytes: 1, modified_at: 200, name: "b.py", status: "modified" as const },
    { path: "c.py", bytes: 1, modified_at: 300, name: "c.py", status: "modified" as const },
  ];

  beforeEach(() => {
    useCommentsMock.mockReturnValue(makeCommentsQuery([]));
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: { available: true, data: multipleFiles },
    } as ReturnType<typeof useWorkspaceChangedFiles>);
  });

  afterEach(() => {
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: {
        available: true,
        data: [
          { path: "file1.py", bytes: 10, modified_at: null, name: "file1.py", status: "modified" },
        ],
      },
    } as ReturnType<typeof useWorkspaceChangedFiles>);
  });

  it("navigates to the previous file on Alt+← when focus is not in a text field", () => {
    // alpha sort: [a.py, b.py, c.py]. Viewing b.py → prevPath=a.py.
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    // Event fired on body — not in any text field.
    // Failure: onNavigateTo was not called (shortcut was incorrectly suppressed).
    fireEvent.keyDown(document.body, { key: "ArrowLeft", altKey: true });

    expect(onNavigateTo).toHaveBeenCalledWith("a.py");
  });

  it("navigates to the next file on Alt+→ when focus is not in a text field", () => {
    // alpha sort: [a.py, b.py, c.py]. Viewing b.py → nextPath=c.py.
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    fireEvent.keyDown(document.body, { key: "ArrowRight", altKey: true });

    expect(onNavigateTo).toHaveBeenCalledWith("c.py");
  });

  it("does not navigate on Alt+← when the event target is a textarea (chat box)", () => {
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    // Simulate option+← typed while the chat textarea has focus.
    // Failure: onNavigateTo is called — word-navigation was stolen.
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "ArrowLeft", altKey: true });

    expect(onNavigateTo).not.toHaveBeenCalled();

    document.body.removeChild(textarea);
  });

  it("does not navigate on Alt+→ when the event target is a textarea (chat box)", () => {
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "ArrowRight", altKey: true });

    expect(onNavigateTo).not.toHaveBeenCalled();

    document.body.removeChild(textarea);
  });

  it("does not navigate on Alt+← when the event target is an input element", () => {
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: "ArrowLeft", altKey: true });

    expect(onNavigateTo).not.toHaveBeenCalled();

    document.body.removeChild(input);
  });

  it("does not navigate on Alt+→ when the event target is an input element", () => {
    const onNavigateTo = vi.fn();
    renderViewer({ open: true, path: "b.py", sort: "alpha", onNavigateTo });

    // Simulate option+→ typed while a search input or other text field has focus.
    // Failure: onNavigateTo is called — word-navigation was stolen.
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: "ArrowRight", altKey: true });

    expect(onNavigateTo).not.toHaveBeenCalled();

    document.body.removeChild(input);
  });
});

describe("FileViewer Escape closes the active tab", () => {
  // Escape closes the open file tab via onCloseTab, but only when the press
  // wasn't already consumed (in-file search or an overlay) and focus isn't in
  // a text field — so dismissing a dialog or hitting Escape while typing never
  // collapses the tab out from under the user.
  function renderWithCloseTab(onCloseTab: () => void) {
    useCommentsMock.mockReturnValue(makeCommentsQuery(undefined));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/"]}>
          <FileViewer
            open
            conversationId="conv_1"
            path="file1.py"
            onClose={vi.fn()}
            onCloseTab={onCloseTab}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("closes the tab on Escape when nothing else handled the key", () => {
    const onCloseTab = vi.fn();
    renderWithCloseTab(onCloseTab);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCloseTab).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape while focus is in a text field", () => {
    const onCloseTab = vi.fn();
    renderWithCloseTab(onCloseTab);
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onCloseTab).not.toHaveBeenCalled();
    document.body.removeChild(input);
  });

  it("ignores Escape already consumed by an overlay (defaultPrevented)", () => {
    const onCloseTab = vi.fn();
    renderWithCloseTab(onCloseTab);
    // A Radix dialog dismisses on Escape in the capture phase and calls
    // preventDefault; mirror that here so the tab-close guard bails.
    const swallow = (e: KeyboardEvent) => {
      if (e.key === "Escape") e.preventDefault();
    };
    window.addEventListener("keydown", swallow, { capture: true });
    fireEvent.keyDown(window, { key: "Escape" });
    window.removeEventListener("keydown", swallow, { capture: true });
    expect(onCloseTab).not.toHaveBeenCalled();
  });
});
