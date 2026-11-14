import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Tabs } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type { PullRequestChangedFile } from "@/hooks/usePullRequests";
import type * as ClipboardModule from "@/lib/clipboard";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  unverifiedPatchHash,
  viewedStorageKey,
  writeViewed,
  type PullRequestIdentity,
} from "@/lib/pullRequestViewed";
import type { Comment } from "@/hooks/useComments";
import { getSeenCommentIds } from "@/hooks/useSeenComments";
import { requestLocalReviewNavigation, useLocalReviewNavigation } from "@/lib/localReviewContext";
import type * as SonnerModule from "sonner";
import { PullRequestChangesTab } from "./PullRequestChangesTab";
import { useChangesViewState } from "./useChangesViewState";

const toast = vi.hoisted(() =>
  Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }),
);
const mocks = vi.hoisted(() => ({
  copyText: vi.fn(async () => {}),
  mobile: false,
  /** The commit list the Commits view reads. */
  commitList: { object: "list", has_more: false, data: [] as unknown[] },
  /** Each commit's patch by sha, as the commit-diff query returns it. */
  commitPatches: new Map<string, string>(),
  /** The PR's changes after each commit, up to the head, by sha. */
  sincePatches: new Map<string, string>(),
  /** Each commit-diff read's commit and until-head sha, in order. */
  commitDiffReads: [] as [string | null, string | null][],
  /** The commit-diff query's error, when a test wants a failed read. */
  commitDiffError: null as Error | null,
  /** The commit list's query cannot run here, so it has no data. */
  commitsUnavailable: false,
  /** The commit list's error, when a test wants a failed read. */
  commitsError: null as Error | null,
  /** The commit list's refetch, to see a retry. */
  commitsRefetch: vi.fn(),
  /** The PR's server lacks the commit routes. */
  commitsUnsupported: false,
  /** A saved review draft, anchored to one file's line, as the composer hook reports it. */
  draftAnchor: null as null | {
    path: string;
    side: "LEFT" | "RIGHT";
    line: number;
    start_line: null;
    head_sha: string;
    base_sha: string;
  },
  animateComment: vi.fn<Element["animate"]>(),
}));
const comments = vi.hoisted(() => ({ data: [] as Comment[], isSuccess: true }));
vi.mock("@/hooks/useComments", () => ({
  useComments: () => ({ ...comments, isFetching: false }),
  useUpdateComment: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteComment: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast,
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "alice" }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => mocks.mobile }));
vi.mock("@/lib/clipboard", async (importOriginal) => ({
  ...(await importOriginal<typeof ClipboardModule>()),
  copyText: mocks.copyText,
}));
vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof UsePullRequestsModule>()),
  // The saved draft belongs to one file, so the anchor is returned for that path only.
  usePullRequestReviewThreadComposer: (_conversationId: string, _prUrl: string, path: string) => ({
    draft: {
      body: "",
      pending: false,
      error: null,
      anchor: mocks.draftAnchor?.path === path ? mocks.draftAnchor : undefined,
    },
    stale: false,
    selectLines: vi.fn(),
    cancel: vi.fn(),
    setBody: vi.fn(),
    post: vi.fn(),
  }),
  // A disabled query has no data, as when the workspace cannot be served.
  usePullRequestCommits: () => ({
    data: mocks.commitsUnavailable || mocks.commitsError ? undefined : mocks.commitList,
    isLoading: false,
    error: mocks.commitsError,
    refetch: mocks.commitsRefetch,
  }),
  usePullRequestCommitsUnsupported: () => mocks.commitsUnsupported,
  usePullRequestCommitDiff: (
    _conversationId: string,
    _prUrl: string,
    sha: string | null,
    untilHeadSha: string | null = null,
  ) => {
    mocks.commitDiffReads.push([sha, untilHeadSha]);
    const patches = untilHeadSha ? mocks.sincePatches : mocks.commitPatches;
    return {
      data: sha ? { object: "session.github.pr_diff", patch: patches.get(sha) } : undefined,
      isLoading: false,
      error: mocks.commitDiffError,
    };
  },
}));
vi.mock("@/components/theme/useResolvedThemeMode", () => ({
  useResolvedThemeMode: () => "light",
}));
vi.mock("./PullRequestReviewThread", () => ({
  PullRequestReviewThread: ({ thread }: { thread: ReviewThread }) => (
    <div data-review-thread={thread.id}>{thread.path}</div>
  ),
}));
// Each added line is a row with its new-file number; annotations render inside the diff.
vi.mock("@pierre/diffs/react", async () => {
  const { Fragment } = await import("react");
  return {
    FileDiff: ({
      fileDiff,
      lineAnnotations,
      renderAnnotation,
    }: {
      fileDiff: { name: string; additionLines: string[] };
      lineAnnotations?: { side: string; lineNumber: number }[];
      renderAnnotation?: (annotation: unknown) => React.ReactNode;
    }) => (
      <div data-testid="diff" data-path={fileDiff.name}>
        {fileDiff.additionLines.map((text, index) => (
          <div key={text} data-line={index + 1} data-line-type="change-addition">
            {text.replace(/\n$/, "")}
          </div>
        ))}
        {lineAnnotations?.map((annotation) => (
          <Fragment key={`${annotation.side}:${annotation.lineNumber}`}>
            {renderAnnotation?.(annotation)}
          </Fragment>
        ))}
      </div>
    ),
  };
});

const changed = (path: string): PullRequestChangedFile => ({
  path,
  name: path,
  status: "modified",
  lines_added: 1,
  lines_removed: 0,
  bytes: null,
  modified_at: null,
});

/** A new file whose lines are all additions, so line n is row n. */
function addedFile(path: string, lines: string[]): string {
  return (
    `diff --git a/${path} b/${path}\nnew file mode 100644\n--- /dev/null\n+++ b/${path}\n` +
    `@@ -0,0 +1,${lines.length} @@\n` +
    lines.map((line) => `+${line}\n`).join("")
  );
}

const A = addedFile("a.ts", ["alpha", "beta"]);
const B = addedFile("b.ts", ["gamma", "needle here"]);
// Over the large-diff threshold, with its one match on the last line.
const C = addedFile("c.ts", [
  ...Array.from({ length: 2499 }, (_, index) => `filler ${index}`),
  "needle at the end",
]);
const FILES = [changed("a.ts"), changed("b.ts"), changed("c.ts")];
const PATCH = A + B + C;

const prUrlFor = (conversationId: string) => `https://github.com/o/r/pull/${conversationId}`;

function renderTab({
  conversationId,
  files = FILES,
  patch = PATCH,
  threads = [],
  listing = {},
  commitHistory = false,
  headRepositoryUrl,
  loading = false,
  selected = true,
}: {
  conversationId: string;
  files?: PullRequestChangedFile[];
  patch?: string;
  threads?: ReviewThread[];
  /** Changed-file list fields beyond the file array, such as truncation or the pinned head. */
  listing?: { has_more?: boolean; total_count?: number | null; head_sha?: string | null };
  /** The provider lists commits, so the Changes toolbar offers the Commits view. */
  commitHistory?: boolean;
  /** A GitHub head repository, so files can be opened on GitHub. */
  headRepositoryUrl?: string;
  loading?: boolean;
  selected?: boolean;
}) {
  const prUrl = prUrlFor(conversationId);
  let patchLoading = loading;
  let tabSelected = selected;
  const tabProps = (current: string) =>
    ({
      conversationId,
      data: {
        selected_pr_url: prUrl,
        capabilities: commitHistory ? { commit_history: true } : {},
        auth: { selected_account: null },
        ...(headRepositoryUrl ? { provider: "github" } : {}),
      },
      pr: {
        url: prUrl,
        head_sha: "head",
        base_sha: "base",
        state: "OPEN",
        head_repository_url: headRepositoryUrl,
      },
      prUrl,
      canEdit: false,
      selected: tabSelected,
      changes: { data: { available: true, data: files, ...listing } },
      prDiff: {
        data: { head_sha: "head", base_sha: "base", patch: current },
        isLoading: patchLoading,
        isFetching: patchLoading,
      },
      reviews: {
        page: { data: threads, head_sha: "head", base_sha: "base" },
        isFetching: false,
        loadComments: () => {},
        loadingComments: null,
      },
    }) as unknown as React.ComponentProps<typeof PullRequestChangesTab>;
  const element = (current: string) => (
    <TooltipProvider>
      <Tabs value={tabSelected ? "changes" : "summary"}>
        <PullRequestChangesTab {...tabProps(current)} />
      </Tabs>
    </TooltipProvider>
  );
  const result = render(element(patch));
  return {
    unmount: result.unmount,
    rerender: (next: string, pending = false) => {
      patchLoading = pending;
      tabSelected = true;
      result.rerender(element(next));
    },
  };
}

const findInput = () => screen.getByRole("textbox", { name: "Find in changes" });
function openFind(query: string) {
  fireEvent.click(screen.getByRole("button", { name: "Find in changes" }));
  fireEvent.change(findInput(), { target: { value: query } });
}
const diffOf = (path: string) => document.querySelector<HTMLElement>(`[data-path="${path}"]`);
const rowOf = (path: string, line: number) =>
  diffOf(path)?.querySelector<HTMLElement>(`[data-line="${line}"]`) ?? null;
// Scoped to the file's section, so header controls elsewhere in the tab can't match.
// The overflow trigger also reports aria-expanded, so take the toggle, which has no label.
const sectionHeader = (path: string, expanded: boolean) =>
  within(document.querySelector<HTMLElement>(`[data-github-file="${path}"]`)!)
    .getAllByRole("button", { expanded })
    .find((button) => !button.hasAttribute("aria-label"))!;

describe("PullRequestChangesTab", () => {
  const originalAnimate = Element.prototype.animate;
  beforeEach(() => {
    localStorage.clear();
    Element.prototype.animate = mocks.animateComment;
  });
  afterEach(() => {
    Element.prototype.animate = originalAnimate;
    comments.data = [];
    comments.isSuccess = true;
    mocks.commitList.data = [];
    mocks.sincePatches.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([
    { selected: true, mode: "changes", reducedMotion: false, earlier: false },
    { selected: false, mode: "changes", reducedMotion: false, earlier: false },
    { selected: true, mode: "commits", reducedMotion: true, earlier: false },
    { selected: true, mode: "since", reducedMotion: false, earlier: false },
    { selected: true, mode: "changes", reducedMotion: false, earlier: true },
  ] as const)(
    "reveals review context from $mode after loading and mounting (selected: $selected, earlier: $earlier)",
    async ({ selected, mode, reducedMotion, earlier }) => {
      const cancelHighlight = vi.fn();
      mocks.animateComment
        .mockReset()
        .mockReturnValue({ cancel: cancelHighlight } as unknown as Animation);
      const media = window.matchMedia("(prefers-reduced-motion: reduce)");
      vi.spyOn(window, "matchMedia").mockReturnValue({ ...media, matches: reducedMotion });
      const conversationId = `local-comment-loading-${mode}-${selected}-${earlier}`;
      const view = renderHook(() => useChangesViewState(conversationId, prUrlFor(conversationId)));
      if (mode === "since") {
        mocks.commitList.data = [{ sha: "earlier-commit" }];
        mocks.sincePatches.set("earlier-commit", B);
        act(() => view.result.current.setSinceSha("earlier-commit"));
      } else {
        act(() => view.result.current.setMode(mode));
      }
      const comment = {
        id: "local-comment",
        conversation_id: conversationId,
        path: "b.ts",
        body: "Review this line",
        status: "draft",
        anchor_metadata: {
          pr_url: prUrlFor(conversationId),
          side: "RIGHT",
          line: 2,
          start_line: null,
          head_sha: earlier ? "previous-head" : "head",
          base_sha: "base",
        },
      } as Comment;
      comments.data = [comment];
      vi.spyOn(Element.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
      const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
      const navigation = renderHook(() => useLocalReviewNavigation(conversationId));
      const tab = renderTab({
        conversationId,
        patch: "",
        loading: true,
        selected,
        commitHistory: true,
        listing: { head_sha: "head" },
      });
      expect(getSeenCommentIds().has(comment.id)).toBe(false);
      vi.useFakeTimers();
      try {
        act(() => requestLocalReviewNavigation(comment));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
        expect(navigation.result.current).toBe(comment);
        expect(scroll).not.toHaveBeenCalled();
        expect(mocks.animateComment).not.toHaveBeenCalled();
        const frame = vi.spyOn(window, "requestAnimationFrame");
        comments.data = [];
        comments.isSuccess = false;
        tab.rerender(PATCH);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
        expect(frame).not.toHaveBeenCalled();
        expect(navigation.result.current).toBe(comment);
        expect(scroll).not.toHaveBeenCalled();
        frame.mockRestore();
      } finally {
        vi.useRealTimers();
      }
      comments.data = [comment];
      comments.isSuccess = true;
      const focus = vi.spyOn(HTMLElement.prototype, "focus").mockImplementationOnce(() => {});
      tab.rerender(PATCH);
      await waitFor(() =>
        expect(document.querySelector('[data-local-review-comment="local-comment"]')).toHaveFocus(),
      );
      await waitFor(() => expect(navigation.result.current).toBeUndefined());
      expect(getSeenCommentIds().has(comment.id)).toBe(true);
      expect(view.result.current.mode).toBe("changes");
      expect(view.result.current.sinceSha).toBeNull();
      expect(view.result.current.shownEarlierCommentPaths.has("b.ts")).toBe(earlier);
      expect(focus.mock.calls.length).toBeGreaterThan(1);
      expect(mocks.animateComment).toHaveBeenCalledWith(
        [
          { borderColor: "var(--ring)", offset: 0 },
          { borderColor: "var(--ring)", offset: reducedMotion ? 1 : 0.8 },
        ],
        { duration: 3000, easing: "ease-out" },
      );
      act(() => requestLocalReviewNavigation(comment));
      await waitFor(() => expect(mocks.animateComment).toHaveBeenCalledTimes(2));
      expect(cancelHighlight).toHaveBeenCalledTimes(1);
      tab.unmount();
      expect(cancelHighlight).toHaveBeenCalledTimes(2);
    },
  );

  it.each(["collapsed", "filtered", "commits"] as const)(
    "keeps feedback unread while its surface is %s",
    (hiddenBy) => {
      const conversationId = `unread-${hiddenBy}`;
      const view = renderHook(() => useChangesViewState(conversationId, prUrlFor(conversationId)));
      comments.data = [
        {
          id: "unread-comment",
          conversation_id: conversationId,
          path: "b.ts",
          body: "Unread feedback",
          status: "draft",
          anchor_metadata: {
            pr_url: prUrlFor(conversationId),
            side: "RIGHT",
            line: 1,
            head_sha: "head",
            base_sha: "base",
          },
        } as Comment,
      ];
      const tab = renderTab({
        conversationId,
        loading: true,
        commitHistory: true,
        listing: { head_sha: "head" },
      });
      if (hiddenBy === "collapsed") {
        fireEvent.click(screen.getByRole("button", { name: "Collapse all diffs" }));
      } else {
        act(() =>
          hiddenBy === "filtered"
            ? view.result.current.setPathFilter("a.ts")
            : view.result.current.setMode("commits"),
        );
      }
      tab.rerender(PATCH);
      expect(screen.queryByText("Unread feedback")).toBeNull();
      expect(getSeenCommentIds().has("unread-comment")).toBe(false);

      if (hiddenBy === "collapsed") {
        fireEvent.click(sectionHeader("b.ts", false));
      } else {
        act(() =>
          hiddenBy === "filtered"
            ? view.result.current.setPathFilter("")
            : view.result.current.setMode("changes"),
        );
      }
      expect(screen.getByText("Unread feedback")).toBeInTheDocument();
      expect(getSeenCommentIds().has("unread-comment")).toBe(true);
    },
  );

  it("toggles earlier comments per file without hiding current comments", async () => {
    const conversationId = "earlier-comments";
    const localComment = (id: string, path: string, head: string, base = "base") =>
      ({
        id,
        conversation_id: conversationId,
        path,
        body: id,
        status: "draft",
        anchor_metadata: {
          pr_url: prUrlFor(conversationId),
          side: "RIGHT",
          line: 1,
          head_sha: head,
          base_sha: base,
        },
      }) as Comment;
    comments.data = [
      localComment("current-a", "a.ts", "head"),
      localComment("earlier-a", "a.ts", "previous-head"),
      localComment("earlier-b", "b.ts", "head", "previous-base"),
      localComment("earlier-gone", "gone.ts", "previous-head"),
    ];
    const thread = {
      id: "outdated-thread",
      path: "a.ts",
      outdated: true,
      resolved: false,
    } as ReviewThread;
    const tab = renderTab({
      conversationId,
      files: [changed("a.ts"), changed("b.ts")],
      patch: A + B,
      threads: [thread],
    });
    const toggleEarlier = async (path: string, label: string) => {
      fireEvent.pointerDown(screen.getByRole("button", { name: `More actions for ${path}` }), {
        button: 0,
      });
      fireEvent.click(await screen.findByRole("menuitem", { name: label }));
    };
    expect(screen.getByText("current-a")).toBeInTheDocument();
    expect(screen.queryByText("earlier-a")).toBeNull();
    expect(screen.queryByText("earlier-b")).toBeNull();
    expect(screen.queryByText("earlier-gone")).toBeNull();
    expect([...getSeenCommentIds()]).toEqual(["current-a"]);
    expect(document.querySelector('[data-review-thread="outdated-thread"]')).toBeNull();

    await toggleEarlier("a.ts", "Show earlier comments");
    expect(screen.getByText("earlier-a")).toBeInTheDocument();
    expect(getSeenCommentIds().has("earlier-a")).toBe(true);
    expect(getSeenCommentIds().has("earlier-b")).toBe(false);
    expect(getSeenCommentIds().has("earlier-gone")).toBe(false);
    expect(document.querySelector('[data-review-thread="outdated-thread"]')).not.toBeNull();
    expect(screen.getByText("current-a")).toBeInTheDocument();
    expect(screen.queryByText("earlier-b")).toBeNull();
    await toggleEarlier("a.ts", "Hide earlier comments");
    expect(screen.queryByText("earlier-a")).toBeNull();
    expect(screen.getByText("current-a")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));
    expect(document.querySelector('[data-review-thread="outdated-thread"]')).not.toBeNull();
    await toggleEarlier("b.ts", "Show earlier comments");
    expect(screen.getByText("earlier-b")).toBeInTheDocument();
    expect(getSeenCommentIds().has("earlier-b")).toBe(true);
    await toggleEarlier("gone.ts", "Show earlier comments");
    expect(screen.getByText("earlier-gone")).toBeInTheDocument();
    expect(getSeenCommentIds().has("earlier-gone")).toBe(true);
    await toggleEarlier("gone.ts", "Hide earlier comments");
    expect(screen.queryByText("earlier-gone")).toBeNull();

    tab.unmount();
    comments.data = comments.data.map((comment) => ({
      ...comment,
      conversation_id: "another-pr",
      anchor_metadata: { ...comment.anchor_metadata!, pr_url: prUrlFor("another-pr") },
    }));
    renderTab({
      conversationId: "another-pr",
      files: [changed("a.ts"), changed("b.ts")],
      patch: A + B,
    });
    expect(screen.getByText("current-a")).toBeInTheDocument();
    expect(screen.queryByText("earlier-b")).toBeNull();
    await toggleEarlier("b.ts", "Show earlier comments");
    expect(screen.getByText("earlier-b")).toBeInTheDocument();
  });

  it("clears the path filter to navigate to a thread on a filtered-out file", () => {
    const conversationId = "changes-tab-nav";
    const prUrl = "https://github.com/o/r/pull/1";
    const store = renderHook(() => useChangesViewState(conversationId, prUrl));
    act(() => store.result.current.setPathFilter("a.ts"));
    const thread = { id: "t1", path: "b.ts", resolved: false } as ReviewThread;
    const props = {
      conversationId,
      data: {
        selected_pr_url: prUrl,
        capabilities: {},
        auth: { selected_account: null },
      },
      pr: { url: prUrl, head_sha: "head", base_sha: "base", state: "OPEN" },
      prUrl,
      canEdit: false,
      selected: true,
      changes: { data: { available: true, data: [changed("a.ts"), changed("b.ts")] } },
      prDiff: { data: { head_sha: "head", base_sha: "base" }, isLoading: false },
      reviews: {
        page: { data: [thread], head_sha: "head", base_sha: "base" },
        isFetching: false,
        loadComments: () => {},
        loadingComments: null,
      },
    } as unknown as React.ComponentProps<typeof PullRequestChangesTab>;
    render(
      <TooltipProvider>
        <Tabs value="changes">
          <PullRequestChangesTab {...props} />
        </Tabs>
      </TooltipProvider>,
    );
    expect(screen.queryByRole("button", { name: /b\.ts/, expanded: true })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));

    expect(store.result.current.pathFilter).toBe("");
    expect(screen.getByRole("button", { name: /b\.ts/, expanded: true })).toBeInTheDocument();
    expect(toast).toHaveBeenCalledWith("Filter cleared to show b.ts");
  });

  it("reveals a match in a collapsed file and centres its line", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderTab({ conversationId: "find-collapsed" });
    fireEvent.click(sectionHeader("b.ts", true));
    expect(diffOf("b.ts")).toBeNull();

    openFind("needle");

    expect(diffOf("b.ts")).not.toBeNull();
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(scroll.mock.contexts).toContain(rowOf("b.ts", 2));
    expect(scroll).toHaveBeenLastCalledWith({ block: "center" });
  });

  it("reveals a match in a section the viewport has not reached", () => {
    renderTab({ conversationId: "find-unmounted" });
    expect(diffOf("b.ts")).toBeNull();

    openFind("needle");

    expect(diffOf("b.ts")).not.toBeNull();
    expect(rowOf("b.ts", 2)).not.toBeNull();
  });

  it("shows a match in a large diff without the Show diff gate", () => {
    renderTab({ conversationId: "find-large" });

    openFind("needle at the end");

    expect(diffOf("c.ts")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Show diff" })).toBeNull();
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
  });

  it("keeps the path filter while typing and navigating, and Show clears it", () => {
    const conversationId = "find-filter";
    const store = renderHook(() => useChangesViewState(conversationId, prUrlFor(conversationId)));
    act(() => store.result.current.setPathFilter("a.ts"));
    renderTab({ conversationId });

    openFind("a");

    expect(screen.getByText("1 of 4")).toBeInTheDocument();
    expect(screen.getByText("+3 in hidden files")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("2 of 4")).toBeInTheDocument();
    expect(store.result.current.pathFilter).toBe("a.ts");

    fireEvent.click(screen.getByRole("button", { name: "Show" }));

    expect(store.result.current.pathFilter).toBe("");
    expect(screen.queryByText(/in hidden files/)).toBeNull();
  });

  it("handles Cmd+F only while focus is inside the Changes tab", () => {
    renderTab({ conversationId: "find-shortcut" });
    expect(fireEvent.keyDown(document.body, { key: "f", ctrlKey: true })).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();

    const header = sectionHeader("a.ts", true);
    header.focus();
    expect(fireEvent.keyDown(header, { key: "f", ctrlKey: true })).toBe(false);
    expect(findInput()).toHaveFocus();
  });

  it("leaves the shortcut to the browser when no toolbar, and so no find bar, is shown", () => {
    renderTab({ conversationId: "find-no-toolbar", files: [], patch: "" });
    const tab = document.querySelector<HTMLElement>('[data-slot="tabs-content"]')!;
    expect(fireEvent.keyDown(tab, { key: "f", ctrlKey: true })).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();
  });

  it("ignores the shortcut while an IME is composing", () => {
    renderTab({ conversationId: "find-ime" });
    const header = sectionHeader("a.ts", true);
    header.focus();
    expect(fireEvent.keyDown(header, { key: "f", ctrlKey: true, isComposing: true })).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();
  });

  it("ignores Enter that confirms an IME composition in the find bar", () => {
    renderTab({ conversationId: "find-ime-enter" });
    openFind("a");
    expect(screen.getByText("1 of 7")).toBeInTheDocument();
    fireEvent.keyDown(findInput(), { key: "Enter", isComposing: true });
    expect(screen.getByText("1 of 7")).toBeInTheDocument();
  });

  it("keeps Ctrl+F as the caret key in a comment on macOS, and Cmd+F opens Find", () => {
    vi.stubGlobal("navigator", { platform: "MacIntel", userAgent: "Mac" });
    renderTab({ conversationId: "find-mac" });
    const tab = document.querySelector<HTMLElement>('[data-slot="tabs-content"]')!;
    const comment = document.createElement("textarea");
    tab.append(comment);
    comment.focus();

    expect(fireEvent.keyDown(comment, { key: "f", ctrlKey: true })).toBe(true);
    expect(comment).toHaveFocus();
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();

    expect(fireEvent.keyDown(comment, { key: "f", metaKey: true })).toBe(false);
    expect(findInput()).toHaveFocus();
  });

  it("follows the shown match across consecutive refreshes", () => {
    const { rerender } = renderTab({ conversationId: "find-consecutive" });
    openFind("needle");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();

    // The match moves from line 2 to line 6 on the first refresh.
    rerender(A + addedFile("b.ts", ["x1", "x2", "x3", "x4", "gamma", "needle here"]) + C);
    expect(screen.getByText("1 of 2")).toBeInTheDocument();

    // A new occurrence at line 1 is nearer the original target (line 2) than the shown
    // one (line 6); the shown match must still win.
    rerender(
      A + addedFile("b.ts", ["needle top", "x2", "x3", "x4", "x5", "gamma", "needle here"]) + C,
    );
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
  });

  it("keeps the query and the nearest match when the patch refreshes", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    const { rerender } = renderTab({ conversationId: "find-refresh" });
    openFind("needle");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(scroll.mock.contexts).toContain(rowOf("b.ts", 2));

    rerender(A + addedFile("b.ts", ["new top", "gamma", "needle here"]) + C);

    expect(findInput()).toHaveValue("needle");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(scroll.mock.contexts).toContain(rowOf("b.ts", 3));
  });

  it("steps through matches with Enter and Shift+Enter, wrapping at the ends", () => {
    renderTab({ conversationId: "find-step" });
    openFind("a");
    expect(screen.getByText("1 of 7")).toBeInTheDocument();

    fireEvent.keyDown(findInput(), { key: "Enter" });
    expect(screen.getByText("2 of 7")).toBeInTheDocument();
    fireEvent.keyDown(findInput(), { key: "Enter", shiftKey: true });
    fireEvent.keyDown(findInput(), { key: "Enter", shiftKey: true });
    expect(screen.getByText("7 of 7")).toBeInTheDocument();
  });

  it("reports no results and an invalid pattern", () => {
    renderTab({ conversationId: "find-invalid" });
    openFind("zzz");
    expect(screen.getByText("No results")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Regular expression" }));
    fireEvent.change(findInput(), { target: { value: "(" } });
    expect(screen.getByText("Invalid")).toBeInTheDocument();
  });

  it("closes on Escape, returns focus to the trigger, and reopens on the shortcut", () => {
    renderTab({ conversationId: "find-escape" });
    openFind("needle");
    fireEvent.keyDown(findInput(), { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Find in changes" })).toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "Find in changes" });
    expect(trigger).toHaveFocus();

    fireEvent.keyDown(trigger, { key: "f", ctrlKey: true });
    expect(findInput()).toHaveFocus();
  });

  it("applies Match case and regular expressions", () => {
    renderTab({ conversationId: "find-options" });
    openFind("NEEDLE");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Match case" }));
    expect(screen.getByText("No results")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Regular expression" }));
    fireEvent.change(findInput(), { target: { value: "needle (here|at)" } });
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });

  it("re-reveals and re-centres a sole match on explicit navigation", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderTab({ conversationId: "find-sole-renav" });
    openFind("needle at the end");
    expect(diffOf("c.ts")).not.toBeNull();
    fireEvent.click(sectionHeader("c.ts", true));
    expect(diffOf("c.ts")).toBeNull();
    expect(sectionHeader("c.ts", false)).toBeInTheDocument();
    const scrollsBefore = scroll.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Next match" }));

    expect(diffOf("c.ts")).not.toBeNull();
    expect(scroll.mock.calls.length).toBeGreaterThan(scrollsBefore);
    expect(scroll.mock.contexts).toContain(rowOf("c.ts", 2500));
  });

  it("steps forward past the last match and back with the Previous button", () => {
    renderTab({ conversationId: "find-buttons" });
    openFind("a");
    expect(screen.getByText("1 of 7")).toBeInTheDocument();
    for (let step = 0; step < 6; step++) {
      fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    }
    expect(screen.getByText("7 of 7")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("1 of 7")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Previous match" }));
    expect(screen.getByText("7 of 7")).toBeInTheDocument();
  });

  it("counts the whole changed-file list when it is truncated", () => {
    renderTab({
      conversationId: "find-truncated",
      files: [changed("a.ts"), changed("b.ts")],
      patch: A + B,
      listing: { has_more: true, total_count: 5 },
    });
    openFind("needle");
    expect(screen.getByText("Search covers the fetched patch: 2 of 5 files")).toBeInTheDocument();
  });

  it("scrolls to a file header when the first match is a path match", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderTab({ conversationId: "find-path-seed" });

    openFind("b.ts");

    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    expect(scroll.mock.contexts).toContain(document.querySelector('[data-github-file="b.ts"]'));
  });

  it("says which part of the list the fetched patch covers", () => {
    renderTab({
      conversationId: "find-coverage",
      files: [changed("a.ts"), changed("b.ts"), changed("img.png")],
      patch: A + B,
    });
    openFind("needle");
    expect(
      screen.getByText("Search covers the fetched patch: 2 of 3 files, 1 without text"),
    ).toBeInTheDocument();
  });

  it("mounts a far-off thread's section and centres the thread", async () => {
    vi.spyOn(Element.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    const thread = {
      id: "t1",
      path: "b.ts",
      side: "RIGHT",
      line: 2,
      start_line: null,
      subject_type: "LINE",
      resolved: false,
      outdated: false,
    } as unknown as ReviewThread;
    renderTab({
      conversationId: "find-thread",
      files: [changed("a.ts"), changed("b.ts")],
      patch: A + B,
      threads: [thread],
    });
    expect(diffOf("b.ts")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));

    expect(diffOf("b.ts")).not.toBeNull();
    await waitFor(() =>
      expect(scroll.mock.contexts).toContain(
        diffOf("b.ts")!.querySelector('[data-review-thread="t1"]'),
      ),
    );
  });
});

const PR_URL = "https://github.com/o/r/pull/1";
const IDENTITY: PullRequestIdentity = {
  provider: "github",
  host: "github.com",
  repository: "o/r",
  number: 1,
};

function viewedTab({
  conversationId,
  head = "head1",
  files = [changed("a.ts"), changed("b.ts")],
  hasMore = false,
  identity = IDENTITY,
  threads = [],
  diffHead = head,
  listHead = head,
  patch,
  commitHistory = false,
}: {
  conversationId: string;
  head?: string;
  files?: PullRequestChangedFile[];
  hasMore?: boolean;
  identity?: PullRequestIdentity | null;
  threads?: ReviewThread[];
  /** The head the loaded patch was fetched for, when it differs from the list's. */
  diffHead?: string;
  /** The list's pinned head; null models a provider that doesn't pin revisions. */
  listHead?: string | null;
  /** The whole-PR patch the diff reports. Without one, every file is unverified. */
  patch?: string;
  commitHistory?: boolean;
}) {
  const props = {
    conversationId,
    data: {
      selected_pr_url: PR_URL,
      base_ref: "main",
      capabilities: commitHistory ? { commit_history: true } : {},
      auth: { selected_account: null },
    },
    pr: { url: PR_URL, number: 1, head_sha: head, base_sha: "base", state: "OPEN" },
    prUrl: PR_URL,
    canEdit: false,
    selected: true,
    changes: { data: { available: true, data: files, has_more: hasMore, head_sha: listHead } },
    prDiff: { data: { head_sha: diffHead, base_sha: "base", patch }, isLoading: false },
    reviews: {
      page: { data: threads, head_sha: head, base_sha: "base" },
      isFetching: false,
      loadComments: () => {},
      loadingComments: null,
    },
    identity,
  } as unknown as React.ComponentProps<typeof PullRequestChangesTab>;
  return (
    <TooltipProvider>
      <Tabs value="changes">
        <PullRequestChangesTab {...props} />
      </Tabs>
    </TooltipProvider>
  );
}

/** One file's patch with a single hunk whose added line is `line`. */
function filePatch(file: string, line: string): string {
  return `diff --git a/${file} b/${file}\nindex 1111111..2222222 100644\n--- a/${file}\n+++ b/${file}\n@@ -1 +1 @@\n-old\n+${line}\n`;
}

/** A patch for a.ts and b.ts; each file's hunk changes only with its own argument. */
function twoFilePatch(aLine: string, bLine = "y"): string {
  return filePatch("a.ts", aLine) + filePatch("b.ts", bLine);
}

/** The file's section root; its first button is the header's collapse toggle. */
function fileSection(path: string): HTMLElement {
  const section = document.querySelector<HTMLElement>(`[data-github-file="${path}"]`);
  if (!section) throw new Error(`No section for ${path}`);
  return section;
}

function isCollapsed(path: string): boolean {
  return fileSection(path).querySelector("button")?.getAttribute("aria-expanded") === "false";
}

/** Whether the file has a section in the diff list (titles also match the sidebar row). */
function isListed(path: string): boolean {
  return document.querySelector(`[data-github-file="${path}"]`) !== null;
}

const viewedBox = (path: string) => screen.getByRole("checkbox", { name: `Viewed ${path}` });

/**
 * Stores marks as another tab would: the entry changes in storage, and this tab hears only the
 * `storage` event.
 */
function markInOtherTab(marks: Record<string, string[]>) {
  const key = viewedStorageKey(window.location.origin, "alice", IDENTITY);
  const newValue = JSON.stringify({ v: 2, marks, updatedAt: Date.now() });
  localStorage.setItem(key, newValue);
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key, newValue, storageArea: localStorage }));
  });
}

describe("PullRequestChangesTab viewed files", () => {
  beforeEach(() => {
    localStorage.clear();
    // A cleared-storage event also drops in-memory fallback values from earlier tests.
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    toast.error.mockClear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("collapses a file when it is marked viewed and keeps the mark after a reload", () => {
    const first = render(viewedTab({ conversationId: "viewed-reload" }));
    fireEvent.click(viewedBox("a.ts"));

    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
    expect(screen.getByText("Viewed,", { exact: true })).toBeInTheDocument();
    expect(screen.getByText("1 / 2 files viewed")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Files viewed" })).toHaveAttribute(
      "aria-valuenow",
      "50",
    );
    first.unmount();

    // A fresh render reads the same localStorage, as a reload would.
    render(viewedTab({ conversationId: "viewed-reload" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
    expect(screen.getByText("1 / 2 files viewed")).toBeInTheDocument();
  });

  it("leaves a file collapsed when its mark is removed", () => {
    render(viewedTab({ conversationId: "viewed-unmark" }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("a.ts"));

    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
    expect(screen.getByText("0 / 2 files viewed")).toBeInTheDocument();
  });

  it("collapses a file that another tab marks viewed", () => {
    render(viewedTab({ conversationId: "viewed-remote" }));
    expect(isCollapsed("a.ts")).toBe(false);

    markInOtherTab({ "a.ts": [unverifiedPatchHash("head1")] });

    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
  });

  it("keeps a file the reader expanded after another tab marked it viewed", () => {
    render(viewedTab({ conversationId: "viewed-remote-expand" }));
    markInOtherTab({ "a.ts": [unverifiedPatchHash("head1")] });
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    expect(isCollapsed("a.ts")).toBe(false);

    // Another mark from the other tab re-reads storage without a new mark on a.ts.
    markInOtherTab({
      "a.ts": [unverifiedPatchHash("head1")],
      "b.ts": [unverifiedPatchHash("head1")],
    });

    expect(isCollapsed("a.ts")).toBe(false);
    expect(isCollapsed("b.ts")).toBe(true);

    // Another tab unmarks a.ts: it stays open. Marking it again collapses it.
    markInOtherTab({ "b.ts": [unverifiedPatchHash("head1")] });
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
    markInOtherTab({
      "a.ts": [unverifiedPatchHash("head1")],
      "b.ts": [unverifiedPatchHash("head1")],
    });
    expect(isCollapsed("a.ts")).toBe(true);
  });

  it("keeps a file the reader expanded when viewed state drops out and returns on the same head", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-remote-reload" }));
    markInOtherTab({ "a.ts": [unverifiedPatchHash("head1")] });
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    expect(isCollapsed("a.ts")).toBe(false);

    rerender(viewedTab({ conversationId: "viewed-remote-reload", identity: null }));
    rerender(viewedTab({ conversationId: "viewed-remote-reload" }));

    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
  });

  it("expands a file that another tab's mark collapsed once the head moves", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-remote-head", head: "head1" }));
    markInOtherTab({ "a.ts": [unverifiedPatchHash("head1")] });
    expect(isCollapsed("a.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-remote-head", head: "head2" }));

    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
  });

  it("hides viewed files together with the path filter", () => {
    const store = renderHook(() => useChangesViewState("viewed-hide", PR_URL));
    const files = [changed("src/a.ts"), changed("src/b.ts"), changed("docs/a.md")];
    render(viewedTab({ conversationId: "viewed-hide", files }));
    fireEvent.click(viewedBox("src/a.ts"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    expect(isListed("src/a.ts")).toBe(false);
    expect(isListed("src/b.ts")).toBe(true);
    expect(isListed("docs/a.md")).toBe(true);

    act(() => store.result.current.setPathFilter("src"));
    expect(isListed("docs/a.md")).toBe(false);
    expect(isListed("src/b.ts")).toBe(true);
    expect(isListed("src/a.ts")).toBe(false);
  });

  it("shows an all-viewed state when hiding viewed files leaves nothing", () => {
    render(viewedTab({ conversationId: "viewed-all" }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("b.ts"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    expect(screen.getByText("All 2 files viewed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show viewed" }));

    expect(screen.getByRole("checkbox", { name: "Hide viewed" })).not.toBeChecked();
    expect(isListed("a.ts")).toBe(true);
  });

  it("moves focus to the next shown file's Viewed box when Hide viewed removes the file just marked", () => {
    render(viewedTab({ conversationId: "viewed-focus-next" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    // The reader's click focuses the box before the file it marks disappears.
    viewedBox("a.ts").focus();
    fireEvent.click(viewedBox("a.ts"));

    expect(isListed("a.ts")).toBe(false);
    expect(viewedBox("b.ts")).toHaveFocus();
  });

  it("moves focus to Hide viewed when the file just marked was the last one shown", () => {
    render(viewedTab({ conversationId: "viewed-focus-last" }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    viewedBox("b.ts").focus();
    fireEvent.click(viewedBox("b.ts"));

    expect(screen.getByText("All 2 files viewed")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Hide viewed" })).toHaveFocus();
  });

  it("resets a changed file's mark on a new head and shows a dismissible notice", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-head", head: "head1", patch: twoFilePatch("new") }),
    );
    fireEvent.click(viewedBox("a.ts"));

    rerender(
      viewedTab({ conversationId: "viewed-head", head: "head2", patch: twoFilePatch("newer") }),
    );
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(screen.getByText("Changed since viewed")).toBeInTheDocument();
    expect(screen.getByText("0 / 2 files viewed")).toBeInTheDocument();
    expect(screen.getByText(/changed files lost their viewed marks/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();
    rerender(
      viewedTab({ conversationId: "viewed-head", head: "head2", patch: twoFilePatch("newer") }),
    );
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();
  });

  it("keeps an unchanged file's mark and collapse on a new head, and flags only the changed file", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-hunks", head: "head1", patch: twoFilePatch("new") }),
    );
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("b.ts"));

    rerender(
      viewedTab({ conversationId: "viewed-hunks", head: "head2", patch: twoFilePatch("new", "z") }),
    );
    expect(viewedBox("a.ts")).toBeChecked();
    expect(viewedBox("b.ts")).not.toBeChecked();
    expect(screen.getByText("Changed since viewed")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 files viewed")).toBeInTheDocument();
    // The unchanged file stays collapsed; the changed one opens for review.
    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(false);
  });

  it.each([
    ["absent from the patch", filePatch("a.ts", "new")],
    ["changed in the patch", twoFilePatch("new", "changed")],
  ])("does not raise the notice when the list omits a marked b.ts that is %s", (_, headPatch) => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-omitted", head: "head1", patch: twoFilePatch("new") }),
    );
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("b.ts"));

    rerender(
      viewedTab({
        conversationId: "viewed-omitted",
        head: "head2",
        files: [changed("a.ts")],
        patch: headPatch,
      }),
    );
    expect(viewedBox("a.ts")).toBeChecked();
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();
    expect(screen.queryByText("Changed since viewed")).toBeNull();
  });

  it("hides viewed state when the list is not pinned to a head", () => {
    // A provider that ignores revision pinning can serve a newer patch than the polled head.
    render(viewedTab({ conversationId: "viewed-unpinned", listHead: null }));

    expect(screen.queryByRole("checkbox", { name: "Viewed a.ts" })).toBeNull();
    expect(screen.queryByText(/files viewed/)).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Hide viewed" })).toBeNull();
  });

  it("waits for the list and the patch to agree on a head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-mismatch", head: "head1", patch: twoFilePatch("new") }),
    );
    fireEvent.click(viewedBox("a.ts"));

    // The list moved to head2 while the patch on screen is still head1.
    rerender(
      viewedTab({
        conversationId: "viewed-mismatch",
        head: "head2",
        diffHead: "head1",
        patch: twoFilePatch("new"),
      }),
    );
    expect(screen.queryByRole("checkbox", { name: "Viewed a.ts" })).toBeNull();
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();

    rerender(
      viewedTab({ conversationId: "viewed-mismatch", head: "head2", patch: twoFilePatch("newer") }),
    );
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(screen.getByText(/changed files lost their viewed marks/)).toBeInTheDocument();
  });

  it("clears the notice once every changed file is marked on the new head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-new-mark", head: "head1", patch: twoFilePatch("new") }),
    );
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("b.ts"));
    rerender(
      viewedTab({
        conversationId: "viewed-new-mark",
        head: "head2",
        patch: twoFilePatch("newer", "z"),
      }),
    );
    expect(screen.getByText(/changed files lost their viewed marks/)).toBeInTheDocument();

    fireEvent.click(viewedBox("a.ts"));
    expect(screen.getByText(/changed files lost their viewed marks/)).toBeInTheDocument();
    fireEvent.click(viewedBox("b.ts"));
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();
    expect(screen.getByText("2 / 2 files viewed")).toBeInTheDocument();
  });

  it("keeps viewed marks when a poll returns the same head", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-same", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));

    rerender(viewedTab({ conversationId: "viewed-same", head: "head1" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(screen.queryByText(/changed files lost their viewed marks/)).toBeNull();
    expect(screen.getByText("1 / 2 files viewed")).toBeInTheDocument();
  });

  it("keeps marks in memory and warns once when storage fails", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    render(viewedTab({ conversationId: "viewed-storage" }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(viewedBox("b.ts"));

    expect(viewedBox("a.ts")).toBeChecked();
    expect(viewedBox("b.ts")).toBeChecked();
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(
      "Viewed files can't be saved on this device.",
      expect.objectContaining({ id: "pr-viewed-persistence" }),
    );
  });

  it("hides the viewed controls until the PR identity is known", () => {
    render(viewedTab({ conversationId: "viewed-unknown", identity: null }));

    expect(screen.queryByRole("checkbox", { name: "Viewed a.ts" })).toBeNull();
    expect(isListed("a.ts")).toBe(true);
    expect(screen.queryByRole("checkbox", { name: "Hide viewed" })).toBeNull();
    expect(screen.queryByText(/files viewed/)).toBeNull();
  });

  it("marks the progress as partial when the file list is truncated", () => {
    render(viewedTab({ conversationId: "viewed-partial", hasMore: true }));

    expect(screen.getByText("0 / 2 files viewed (partial)")).toBeInTheDocument();
  });

  it("un-hides a viewed file when navigating to its conversation", () => {
    const store = renderHook(() => useChangesViewState("viewed-thread", PR_URL));
    const thread = { id: "t1", path: "a.ts", resolved: false } as ReviewThread;
    render(viewedTab({ conversationId: "viewed-thread", threads: [thread] }));
    fireEvent.click(viewedBox("a.ts"));
    act(() => store.result.current.setHideViewed(true));
    expect(isListed("a.ts")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));

    expect(store.result.current.hideViewed).toBe(false);
    expect(isListed("a.ts")).toBe(true);
  });

  it("keeps a matching path filter when navigating to a viewed file", () => {
    const store = renderHook(() => useChangesViewState("viewed-thread-filter", PR_URL));
    const thread = { id: "t1", path: "a.ts", resolved: false } as ReviewThread;
    render(viewedTab({ conversationId: "viewed-thread-filter", threads: [thread] }));
    fireEvent.click(viewedBox("a.ts"));
    act(() => store.result.current.setPathFilter("a.ts"));
    act(() => store.result.current.setHideViewed(true));
    expect(isListed("a.ts")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Next unresolved conversation" }));

    expect(store.result.current.pathFilter).toBe("a.ts");
    expect(store.result.current.hideViewed).toBe(false);
    expect(isListed("a.ts")).toBe(true);
    expect(isCollapsed("a.ts")).toBe(false);
  });

  it("keeps files the reader collapsed with Collapse all on a new head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-collapse-all", head: "head1" }),
    );
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(screen.getByRole("button", { name: "Collapse all diffs" }));
    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-collapse-all", head: "head2" }));

    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(true);
  });

  it("expands files collapsed by old marks on a new head but keeps manual collapses", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-collapse", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(fileSection("b.ts").querySelector("button")!);
    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(true);

    // Another tab saved a mark on a.ts at head2. Loading H2 keeps a.ts
    // collapsed, and that collapse still belongs to the mark: a fresh H3 expands it again.
    writeViewed(viewedStorageKey(window.location.origin, "alice", IDENTITY), {
      "a.ts": [unverifiedPatchHash("head2")],
    });
    rerender(viewedTab({ conversationId: "viewed-collapse", head: "head2" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-collapse", head: "head3" }));
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
    expect(isCollapsed("b.ts")).toBe(true);
  });

  it("expands a mark-collapsed file on a fresh head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-collapse-fresh", head: "head1" }),
    );
    fireEvent.click(viewedBox("a.ts"));
    expect(isCollapsed("a.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-collapse-fresh", head: "head4" }));

    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
  });

  it("keeps a file the reader collapsed before marking it collapsed on a new head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-manual-first", head: "head1" }),
    );
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    fireEvent.click(viewedBox("a.ts"));
    expect(isCollapsed("a.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-manual-first", head: "head2" }));

    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
  });

  it("keeps a file collapsed on a new head after the reader expands and recollapses it", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-manual-again", head: "head1" }),
    );
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    expect(isCollapsed("a.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "viewed-manual-again", head: "head2" }));

    expect(isCollapsed("a.ts")).toBe(true);
  });

  it("keeps a manual collapse made before identity loads when viewed files seed later", () => {
    const origin = window.location.origin;
    writeViewed(viewedStorageKey(origin, "alice", IDENTITY), {
      "a.ts": [unverifiedPatchHash("head1")],
    });
    const { rerender } = render(
      viewedTab({ conversationId: "viewed-late-identity", identity: null }),
    );
    fireEvent.click(fileSection("b.ts").querySelector("button")!);
    expect(isCollapsed("b.ts")).toBe(true);
    expect(isCollapsed("a.ts")).toBe(false);

    rerender(viewedTab({ conversationId: "viewed-late-identity" }));

    expect(isCollapsed("a.ts")).toBe(true);
    expect(isCollapsed("b.ts")).toBe(true);
  });
});

describe("PullRequestChangesTab file actions and filter", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.mobile = false;
    // Viewed marks from other tests share this identity and head, so start from empty storage.
    localStorage.clear();
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
  });

  const ACME_PR_URL = "https://github.com/acme/app/pull/1";
  const FORK_URL = "https://github.com/fork/app";

  /** The Changes tab's props for one PR; each test uses its own conversation id because view state is module-level. */
  function tabProps(
    conversationId: string,
    {
      files = [changed("src/app.ts"), changed("src/util.ts"), changed("README.md")],
      pr = {},
      threads = [],
    }: {
      files?: PullRequestChangedFile[];
      pr?: Partial<React.ComponentProps<typeof PullRequestChangesTab>["pr"]>;
      threads?: ReviewThread[];
    } = {},
  ) {
    return {
      conversationId,
      identity: null,
      data: {
        selected_pr_url: ACME_PR_URL,
        provider: "github",
        capabilities: {},
        auth: { selected_account: null },
      },
      pr: {
        url: ACME_PR_URL,
        head_sha: "head",
        base_sha: "base",
        state: "OPEN",
        head_repository_url: FORK_URL,
        ...pr,
      },
      prUrl: ACME_PR_URL,
      canEdit: false,
      selected: true,
      changes: {
        isLoading: false,
        error: null,
        data: { available: true, data: files, head_sha: "head", base_sha: "base" },
      },
      prDiff: { data: { head_sha: "head", base_sha: "base" }, isLoading: false },
      reviews: {
        page: { data: threads, head_sha: "head", base_sha: "base" },
        isFetching: false,
        loadComments: () => {},
        loadingComments: null,
      },
    } as unknown as React.ComponentProps<typeof PullRequestChangesTab>;
  }

  function tree(props: React.ComponentProps<typeof PullRequestChangesTab>) {
    return (
      <TooltipProvider>
        <Tabs value="changes">
          <PullRequestChangesTab {...props} />
        </Tabs>
      </TooltipProvider>
    );
  }

  /** The stacked list's sections, by path. */
  function listedPaths(container: HTMLElement) {
    return [...container.querySelectorAll("[data-github-file]")].map((element) =>
      element.getAttribute("data-github-file"),
    );
  }

  /** The header button that expands or collapses one file's section. */
  function fileToggle(container: HTMLElement, path: string) {
    const toggle = container.querySelector<HTMLElement>(
      `[data-github-file="${path}"] button[aria-expanded]`,
    );
    if (!toggle) throw new Error(`No section for ${path}`);
    return toggle;
  }

  function openMenu(path: string) {
    fireEvent.pointerDown(screen.getByRole("button", { name: `More actions for ${path}` }), {
      button: 0,
    });
  }

  it("filters the tree and list, keeps collapse state, and clears on Escape", () => {
    const conversationId = "changes-tab-filter";
    const { container } = render(tree(tabProps(conversationId)));
    const toggle = () => fileToggle(container, "src/app.ts");
    expect(toggle()).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle());
    expect(toggle()).toHaveAttribute("aria-expanded", "false");

    const filter = screen.getByRole("searchbox", { name: "Filter changed files" });
    fireEvent.change(filter, { target: { value: "UTIL" } });
    expect(listedPaths(container)).toEqual(["src/util.ts"]);
    expect(screen.queryAllByTitle("src/app.ts")).toHaveLength(0);
    expect(screen.getAllByTitle("src/util.ts")).toHaveLength(2);

    fireEvent.change(filter, { target: { value: "src\\" } });
    expect(listedPaths(container)).toEqual(["src/app.ts", "src/util.ts"]);

    fireEvent.keyDown(filter, { key: "Escape" });
    expect(filter).toHaveValue("");
    expect(listedPaths(container)).toEqual(["src/app.ts", "src/util.ts", "README.md"]);
    expect(toggle()).toHaveAttribute("aria-expanded", "false");
  });

  it("shows an empty state for no matches and clears the filter from it", () => {
    const conversationId = "changes-tab-empty";
    const { container } = render(tree(tabProps(conversationId)));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "nothing-here" },
    });
    expect(screen.getByText('No changed files match "nothing-here"')).toBeInTheDocument();
    expect(listedPaths(container)).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(listedPaths(container)).toEqual(["src/app.ts", "src/util.ts", "README.md"]);
  });

  it("matches the previous path of a renamed file", () => {
    const conversationId = "changes-tab-rename-filter";
    const renamed = {
      ...changed("lib/new.ts"),
      status: "renamed" as const,
      previous_path: "src/old.ts",
    };
    const { container } = render(
      tree(tabProps(conversationId, { files: [renamed, changed("README.md")] })),
    );
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "src/old" },
    });
    expect(listedPaths(container)).toEqual(["lib/new.ts"]);
  });

  it("copies the current path and the previous path of a renamed file", async () => {
    const conversationId = "changes-tab-copy";
    const renamed = {
      ...changed("lib/new.ts"),
      status: "renamed" as const,
      previous_path: "src/old.ts",
    };
    render(tree(tabProps(conversationId, { files: [renamed] })));

    fireEvent.click(screen.getByRole("button", { name: "Copy path lib/new.ts" }));
    expect(mocks.copyText).toHaveBeenLastCalledWith("lib/new.ts");
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Copied path"));

    openMenu("lib/new.ts");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Copy previous path" }));
    expect(mocks.copyText).toHaveBeenLastCalledWith("src/old.ts");
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Copied previous path"));
  });

  it("keeps Open on the rendered head after the polled head moves", () => {
    const conversationId = "changes-tab-open-pinned";
    const props = tabProps(conversationId, { files: [changed("src/app.ts")] });
    const { rerender } = render(tree(props));
    openMenu("src/app.ts");
    expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toHaveAttribute(
      "href",
      `${FORK_URL}/blob/head/src/app.ts`,
    );

    rerender(
      tree(
        tabProps(conversationId, {
          files: [changed("src/app.ts")],
          pr: { head_sha: "newer" },
        }),
      ),
    );
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    openMenu("src/app.ts");
    expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toHaveAttribute(
      "href",
      `${FORK_URL}/blob/head/src/app.ts`,
    );
  });

  it.each([
    {
      provider: "gitlab",
      display: {
        id: "gitlab",
        display_name: "GitLab",
        request_name: "merge request",
        number_prefix: "!",
      },
      prUrl: "https://gitlab.com/acme/app/-/merge_requests/1",
      fork: "https://gitlab.com/fork/app",
      label: "Open in GitLab",
      href: "https://gitlab.com/fork/app/-/blob/head/src/app.ts",
    },
    {
      provider: "azure_devops",
      display: {
        id: "azure_devops",
        display_name: "Azure DevOps",
        request_name: "pull request",
        number_prefix: "#",
      },
      prUrl: "https://dev.azure.com/acme/web/_git/app/pullrequest/1",
      fork: "https://dev.azure.com/fork/web/_git/app",
      label: "Open in Azure DevOps",
      href: "https://dev.azure.com/fork/web/_git/app?path=/src/app.ts&version=GChead",
    },
  ])("labels and links Open for $provider", ({ provider, display, prUrl, fork, label, href }) => {
    const props = tabProps(`changes-tab-${provider}`, {
      files: [changed("src/app.ts")],
      pr: { url: prUrl, head_repository_url: fork },
    });
    props.data = { ...props.data, provider, provider_display: display } as typeof props.data;
    render(tree(props));
    openMenu("src/app.ts");
    expect(screen.getByRole("menuitem", { name: label })).toHaveAttribute("href", href);
  });

  it("disables Open when the list reports no pinned revision, even with a polled head", () => {
    const conversationId = "changes-tab-open-unpinned";
    const props = tabProps(conversationId, { files: [changed("src/app.ts")] });
    props.changes = {
      ...props.changes,
      data: { ...props.changes.data, head_sha: undefined, base_sha: undefined },
    } as typeof props.changes;
    render(tree(props));
    openMenu("src/app.ts");
    const disabled = screen.getByRole("menuitem", { name: /Open in GitHub/ });
    expect(disabled).toHaveAttribute("data-disabled");
    expect(disabled).toHaveTextContent("Revision unknown");
    expect(disabled).not.toHaveAttribute("href");
  });

  it("disables Open for a deleted file when the list reports no base revision", () => {
    const conversationId = "changes-tab-open-no-base";
    const props = tabProps(conversationId, {
      files: [{ ...changed("old.ts"), status: "deleted" }, changed("src/app.ts")],
    });
    props.changes = {
      ...props.changes,
      data: { ...props.changes.data, base_sha: undefined },
    } as typeof props.changes;
    render(tree(props));
    openMenu("old.ts");
    const disabled = screen.getByRole("menuitem", { name: /Open in GitHub/ });
    expect(disabled).toHaveAttribute("data-disabled");
    expect(disabled).toHaveTextContent("Revision unknown");
    expect(disabled).not.toHaveAttribute("href");
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    openMenu("src/app.ts");
    expect(screen.getByRole("menuitem", { name: /Open in GitHub/ })).toHaveAttribute(
      "href",
      `${FORK_URL}/blob/head/src/app.ts`,
    );
  });

  it("opens a deleted file at the base revision in the base repository", () => {
    const conversationId = "changes-tab-open-deleted";
    render(
      tree(tabProps(conversationId, { files: [{ ...changed("old.ts"), status: "deleted" }] })),
    );
    openMenu("old.ts");
    expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toHaveAttribute(
      "href",
      `${ACME_PR_URL.split("/pull/")[0]}/blob/base/old.ts`,
    );
  });

  it("disables Open for a non-deleted file when the head repository is absent", () => {
    const conversationId = "changes-tab-open-absent";
    // An explicit undefined reads like a host that predates the field.
    render(
      tree(
        tabProps(conversationId, {
          files: [changed("src/app.ts"), { ...changed("old.ts"), status: "deleted" }],
          pr: { head_repository_url: undefined },
        }),
      ),
    );
    openMenu("src/app.ts");
    const disabled = screen.getByRole("menuitem", { name: /Open in GitHub/ });
    expect(disabled).toHaveAttribute("data-disabled");
    expect(disabled).toHaveTextContent("Source repository unavailable");
    expect(disabled).not.toHaveAttribute("href");

    // A deleted file still opens from the base repository at the base revision.
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    openMenu("old.ts");
    expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toHaveAttribute(
      "href",
      "https://github.com/acme/app/blob/base/old.ts",
    );
  });

  it("shows the revision as unknown when no revision is known", () => {
    const conversationId = "changes-tab-open-no-revision";
    const props = tabProps(conversationId, {
      files: [changed("src/app.ts")],
      pr: { head_sha: undefined },
    });
    props.changes = {
      ...props.changes,
      data: { ...props.changes.data, head_sha: undefined, base_sha: undefined },
    } as typeof props.changes;
    render(tree(props));
    openMenu("src/app.ts");
    const disabled = screen.getByRole("menuitem", { name: /Open in GitHub/ });
    expect(disabled).toHaveAttribute("data-disabled");
    expect(disabled).toHaveTextContent("Revision unknown");
    expect(disabled).not.toHaveAttribute("href");
  });

  it("puts every file action in the overflow menu on mobile", () => {
    mocks.mobile = true;
    try {
      const conversationId = "changes-tab-mobile";
      const { container } = render(tree(tabProps(conversationId)));
      expect(screen.queryByRole("button", { name: "Copy path src/app.ts" })).toBeNull();
      openMenu("src/app.ts");
      expect(screen.getByRole("menuitem", { name: "Copy path" })).toBeInTheDocument();
      expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toBeInTheDocument();
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

      // The sidebar starts collapsed on mobile: the filter opens inline above the list.
      expect(screen.queryByRole("searchbox", { name: "Filter changed files" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Show file filter" }));
      fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
        target: { value: "readme" },
      });
      expect(listedPaths(container)).toEqual(["README.md"]);
      // Hiding the inline filter clears it, so the list is never narrowed out of sight.
      fireEvent.click(screen.getByRole("button", { name: "Hide file filter" }));
      expect(screen.queryByRole("searchbox", { name: "Filter changed files" })).toBeNull();
      expect(listedPaths(container)).toEqual(["src/app.ts", "src/util.ts", "README.md"]);
    } finally {
      mocks.mobile = false;
    }
  });

  it("reports a failed copy and keeps the sidebar filter open after collapse", async () => {
    mocks.copyText.mockRejectedValueOnce(new Error("denied"));
    const conversationId = "changes-tab-copy-failure";
    render(tree(tabProps(conversationId)));
    fireEvent.click(screen.getByRole("button", { name: "Copy path src/app.ts" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn’t copy path"));

    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "util" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Hide file list" }));
    const inline = screen.getByRole("searchbox", { name: "Filter changed files" });
    expect(inline).toHaveValue("util");
    // Clearing with Escape must keep the same input mounted, so focus stays in it.
    fireEvent.keyDown(inline, { key: "Escape" });
    expect(screen.getByRole("searchbox", { name: "Filter changed files" })).toBe(inline);
    expect(inline).toHaveValue("");
  });

  it("closes the inline filter when the sidebar expands again", () => {
    const { container } = render(tree(tabProps("changes-tab-inline-close")));
    fireEvent.click(screen.getByRole("button", { name: "Hide file list" }));
    expect(screen.queryByRole("searchbox", { name: "Filter changed files" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show file list" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "util" },
    });
    expect(listedPaths(container)).toEqual(["src/util.ts"]);
    fireEvent.click(screen.getByRole("button", { name: "Hide file list" }));
    expect(screen.getByRole("searchbox", { name: "Filter changed files" })).toHaveValue("util");
    fireEvent.keyDown(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      key: "Escape",
    });
    fireEvent.click(screen.getByRole("button", { name: "Show file list" }));
    fireEvent.click(screen.getByRole("button", { name: "Hide file list" }));
    expect(screen.queryByRole("searchbox", { name: "Filter changed files" })).toBeNull();
  });

  it("groups threads on filtered-out files apart from earlier conversations", () => {
    const conversationId = "changes-tab-threads";
    const threadOn = (id: string, path: string) => ({ id, path, resolved: false }) as ReviewThread;
    const threads = [
      threadOn("t-app", "src/app.ts"),
      threadOn("t-util", "src/util.ts"),
      threadOn("t-gone", "deleted-upstream.ts"),
    ];
    const { container } = render(tree(tabProps(conversationId, { threads })));
    expect(container.querySelector('[data-review-thread="t-util"]')).not.toBeNull();
    expect(screen.queryByText("Earlier conversations and unavailable diffs")).not.toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "src/app" },
    });

    expect(container.querySelector('[data-review-thread="t-app"]')).not.toBeNull();
    expect(container.querySelector('[data-review-thread="t-util"]')).toBeNull();
    const earlier = screen.getByText("Earlier conversations and unavailable diffs").parentElement;
    expect(earlier!.querySelector('[data-review-thread="t-gone"]')).not.toBeNull();
    expect(earlier!.querySelector('[data-review-thread="t-util"]')).toBeNull();
  });

  it("keeps threads on viewed files out of the earlier group while hide-viewed is on", () => {
    const threads = [
      { id: "t-a", path: "a.ts", resolved: false },
      { id: "t-b", path: "b.ts", resolved: false },
    ] as ReviewThread[];
    const { container } = render(viewedTab({ conversationId: "threads-viewed", threads }));
    fireEvent.click(viewedBox("b.ts"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    expect(container.querySelector('[data-review-thread="t-b"]')).toBeNull();
    expect(container.querySelector('[data-review-thread="t-a"]')).not.toBeNull();
    expect(screen.queryByText("Earlier conversations and unavailable diffs")).toBeNull();
  });
});

describe("PullRequestChangesTab commits", () => {
  const SHA_A = "a".repeat(40);
  const SHA_B = "b".repeat(40);
  const commit = (sha: string, title: string) => ({
    object: "session.github.commit",
    sha,
    title,
    author: "Ada",
    date: "2026-11-01T10:00:00Z",
  });

  beforeEach(() => {
    // Viewed marks are keyed by identity and head, so one test's marks would leak into the next.
    localStorage.clear();
    mocks.commitList = {
      object: "list",
      has_more: false,
      data: [commit(SHA_A, "Add a.ts"), commit(SHA_B, "Add b.ts")],
    };
    mocks.commitPatches.clear();
    mocks.commitPatches.set(SHA_A, A);
    mocks.commitPatches.set(SHA_B, B);
    mocks.sincePatches.clear();
    mocks.commitDiffReads = [];
    mocks.commitDiffError = null;
    mocks.commitsError = null;
    mocks.commitsRefetch.mockClear();
    mocks.draftAnchor = null;
    mocks.commitsUnsupported = false;
  });

  it("lists commits, opens one with its own files, and steps between commits", () => {
    renderTab({
      conversationId: "commits-open",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    expect(isListed("a.ts")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add b\.ts/ }));

    expect(isListed("b.ts")).toBe(true);
    expect(isListed("a.ts")).toBe(false);
    expect(screen.getByRole("button", { name: "Next commit" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Previous commit" }));
    expect(isListed("a.ts")).toBe(true);
    expect(isListed("b.ts")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Back to commits" }));
    expect(screen.getByRole("button", { name: /Add a\.ts/ })).toBeInTheDocument();
  });

  it("shows only the changes since a picked commit, without review controls, then all again", () => {
    mocks.commitList = {
      object: "list",
      has_more: false,
      data: [
        { ...commit(SHA_A, "Parent work"), head_of: [{ number: 41, url: null, state: "OPEN" }] },
        commit(SHA_B, "Own work"),
      ],
    };
    mocks.sincePatches.set(SHA_A, B);
    renderTab({
      conversationId: "changes-since",
      commitHistory: true,
      listing: { head_sha: SHA_B },
      headRepositoryUrl: "https://github.com/fork/app",
    });
    expect(isListed("a.ts")).toBe(true);

    fireEvent.click(screen.getByRole("combobox", { name: "Changes since" }));
    // Nothing comes after the head, so it isn't offered.
    expect(screen.queryByRole("option", { name: /Own work/ })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: /Head of #41.*Parent work/ }));

    expect(mocks.commitDiffReads.at(-1)).toEqual([SHA_A, SHA_B]);
    expect(isListed("b.ts")).toBe(true);
    expect(isListed("a.ts")).toBe(false);
    expect(screen.queryByRole("checkbox", { name: "Hide viewed" })).toBeNull();
    // The narrowed patch ends at the head, so its files open there.
    fireEvent.pointerDown(screen.getByRole("button", { name: "More actions for b.ts" }), {
      button: 0,
    });
    expect(screen.getByRole("menuitem", { name: "Open in GitHub" })).toHaveAttribute(
      "href",
      `https://github.com/fork/app/blob/${SHA_B}/b.ts`,
    );
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    fireEvent.click(screen.getByRole("combobox", { name: "Changes since" }));
    fireEvent.click(screen.getByRole("option", { name: "PR base (all changes)" }));
    expect(isListed("a.ts")).toBe(true);
  });

  it("offers the newest listed commit when GitHub capped the list before the head", () => {
    mocks.commitList = {
      object: "list",
      has_more: true,
      data: [commit(SHA_A, "Early work"), commit(SHA_B, "Last listed work")],
    };
    renderTab({
      conversationId: "changes-since-capped",
      commitHistory: true,
      listing: { head_sha: "c".repeat(40) },
    });

    fireEvent.click(screen.getByRole("combobox", { name: "Changes since" }));
    expect(screen.getByRole("option", { name: /Last listed work/ })).toBeInTheDocument();
    expect(screen.getByText(/Only the first \d+ commits are listed/)).toBeInTheDocument();
  });

  it("says why the commits failed to load and retries when the picker opens again", () => {
    mocks.commitsError = new Error("GitHub could not load the PR's commits.");
    renderTab({
      conversationId: "changes-since-failed",
      commitHistory: true,
      listing: { head_sha: SHA_B },
    });

    fireEvent.click(screen.getByRole("combobox", { name: "Changes since" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/could not load the PR's commits/);
    expect(mocks.commitsRefetch).toHaveBeenCalled();
  });

  it("hides the viewed controls and marks in a commit view, and keeps the marks for Changes", () => {
    render(viewedTab({ conversationId: "commits-viewed", commitHistory: true }));
    fireEvent.click(viewedBox("a.ts"));
    expect(screen.getAllByText(/^Viewed,/).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add a\.ts/ }));
    expect(screen.queryByRole("checkbox", { name: "Hide viewed" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /^Viewed / })).toBeNull();
    expect(screen.queryAllByText(/^Viewed,/)).toHaveLength(0);
    expect(isCollapsed("a.ts")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);
  });

  it("keeps the PR's Hide viewed when Find shows a commit file the path filter hides", () => {
    render(viewedTab({ conversationId: "commits-hide-viewed", commitHistory: true }));
    fireEvent.click(viewedBox("a.ts"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Hide viewed" }));

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add a\.ts/ }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter changed files" }), {
      target: { value: "nothing-matches" },
    });
    openFind("a.ts");
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(isListed("a.ts")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(screen.getByRole("checkbox", { name: "Hide viewed" })).toBeChecked();
  });

  it("keeps the PR's collapse and reveal state through commit toggles, and resets it on a new head", () => {
    const { rerender } = render(
      viewedTab({ conversationId: "commits-isolated", commitHistory: true }),
    );
    fireEvent.click(viewedBox("a.ts"));
    expect(isCollapsed("a.ts")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add a\.ts/ }));
    expect(isCollapsed("a.ts")).toBe(false);
    // The commit's own collapse toggles, then a sidebar jump reveals the collapsed file, in the commit only.
    fireEvent.click(fileSection("a.ts").querySelector("button")!);
    expect(isCollapsed("a.ts")).toBe(true);
    fireEvent.click(document.querySelector<HTMLButtonElement>('button[title="a.ts"]')!);
    expect(isCollapsed("a.ts")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(isCollapsed("a.ts")).toBe(true);

    rerender(viewedTab({ conversationId: "commits-isolated", commitHistory: true, head: "head2" }));
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(isCollapsed("a.ts")).toBe(false);
  });

  it("keeps a saved PR draft in Changes, leaves it out of a commit view, and returns it unchanged", () => {
    mocks.draftAnchor = {
      path: "a.ts",
      side: "RIGHT",
      line: 2,
      start_line: null,
      head_sha: "head",
      base_sha: "base",
    };
    renderTab({
      conversationId: "commits-draft",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    expect(screen.getByRole("region", { name: "New review conversation" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add a\.ts/ }));
    expect(isListed("a.ts")).toBe(true);
    expect(screen.queryByRole("region", { name: "New review conversation" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Changes" }));
    expect(screen.getByRole("region", { name: "New review conversation" })).toBeInTheDocument();
  });

  it("returns a saved commit view to Changes and hides the switch when the server lacks the routes", () => {
    const { rerender } = renderTab({
      conversationId: "commits-unsupported",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    expect(screen.getByText("2 commits")).toBeInTheDocument();

    // A capable host behind an older server: the 404 from the commit routes marks the PR.
    mocks.commitsUnsupported = true;
    rerender(PATCH);

    expect(screen.queryByRole("button", { name: "Commits" })).toBeNull();
    expect(screen.queryByText("2 commits")).toBeNull();
    expect(isListed("a.ts")).toBe(true);
    expect(toast).toHaveBeenCalledWith(
      "Commits need an updated Omnigent server.",
      expect.objectContaining({ id: "pr-commits-unsupported" }),
    );

    // The saved commit view did not survive: once the server has the routes, Changes is shown.
    mocks.commitsUnsupported = false;
    rerender(PATCH);
    expect(screen.getByRole("button", { name: "Commits" })).toBeInTheDocument();
    expect(screen.queryByText("2 commits")).toBeNull();
  });

  it("keeps the commit search while the reader moves between the list and a commit", () => {
    renderTab({
      conversationId: "commits-query",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search commits" }), {
      target: { value: "add b" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Add b\.ts/ }));
    fireEvent.click(screen.getByRole("button", { name: "Back to commits" }));

    expect(screen.getByRole("searchbox", { name: "Search commits" })).toHaveValue("add b");
    expect(screen.queryByRole("button", { name: /Add a\.ts/ })).toBeNull();
  });

  it("shows no file-filter toggle on the narrow commit list, where its input is not shown", () => {
    mocks.mobile = true;
    try {
      renderTab({
        conversationId: "commits-narrow",
        commitHistory: true,
        listing: { head_sha: "head" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Commits" }));

      expect(screen.queryByRole("button", { name: "Show file filter" })).toBeNull();
      expect(screen.getByRole("searchbox", { name: "Search commits" })).toBeInTheDocument();
    } finally {
      mocks.mobile = false;
    }
  });

  it("says the history is unavailable, not empty, when the commit query cannot run here", () => {
    mocks.commitsUnavailable = true;
    try {
      renderTab({
        conversationId: "commits-offline",
        commitHistory: true,
        listing: { head_sha: "head" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Commits" }));

      expect(screen.getByText("Commits are unavailable.")).toBeInTheDocument();
    } finally {
      mocks.commitsUnavailable = false;
    }
  });

  it("offers no Commits view when the provider does not list commits", () => {
    renderTab({ conversationId: "commits-absent", listing: { head_sha: "head" } });

    expect(screen.queryByRole("button", { name: "Commits" })).toBeNull();
  });

  it("offers no Commits view while the file list is not pinned to a head", () => {
    renderTab({ conversationId: "commits-unpinned", commitHistory: true });

    expect(screen.queryByRole("button", { name: "Commits" })).toBeNull();
    expect(isListed("a.ts")).toBe(true);
  });

  it("leaves Ctrl+F to the browser on the commit list, which has no find bar", () => {
    renderTab({
      conversationId: "commits-find",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));

    const search = screen.getByRole("searchbox", { name: "Search commits" });
    expect(fireEvent.keyDown(search, { key: "f", ctrlKey: true })).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();
  });

  it("returns to the commit list when the open commit leaves the loaded list", () => {
    const { rerender } = renderTab({
      conversationId: "commits-gone",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add b\.ts/ }));

    mocks.commitList = { object: "list", has_more: false, data: [commit(SHA_A, "Add a.ts")] };
    rerender(B);

    expect(screen.getByRole("button", { name: /Add a\.ts/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Back to commits" })).toBeNull();
  });

  it("shows a failed commit-diff read in place of the commit's files", () => {
    mocks.commitDiffError = new Error("Commit diff unavailable");
    renderTab({
      conversationId: "commits-error",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Add b\.ts/ }));

    expect(screen.getByRole("alert")).toHaveTextContent("Commit diff unavailable");
  });

  it("says a commit with no file changes has none, rather than the PR's list", () => {
    const shaEmpty = "e".repeat(40);
    mocks.commitList = {
      object: "list",
      has_more: false,
      data: [commit(shaEmpty, "Empty commit")],
    };
    mocks.commitPatches.set(shaEmpty, "");
    renderTab({
      conversationId: "commits-empty",
      commitHistory: true,
      listing: { head_sha: "head" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Empty commit/ }));

    expect(screen.getAllByText("This commit has no file changes.").length).toBeGreaterThan(0);
    expect(screen.queryByText("No changes vs base.")).toBeNull();
  });

  it("disables a deleted file's Open in a commit view, since the commit has no page for it", () => {
    const deletedPatch =
      "diff --git a/gone.ts b/gone.ts\ndeleted file mode 100644\n--- a/gone.ts\n+++ /dev/null\n" +
      "@@ -1 +0,0 @@\n-bye\n";
    const shaC = "c".repeat(40);
    mocks.commitList = { object: "list", has_more: false, data: [commit(shaC, "Remove gone.ts")] };
    mocks.commitPatches.set(shaC, deletedPatch);
    renderTab({
      conversationId: "commits-deleted",
      commitHistory: true,
      listing: { head_sha: "head" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Commits" }));
    fireEvent.click(screen.getByRole("button", { name: /Remove gone\.ts/ }));
    fireEvent.pointerDown(screen.getByRole("button", { name: "More actions for gone.ts" }), {
      button: 0,
    });

    expect(screen.getByText("Deleted in this commit")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Open in/ })).toHaveAttribute("data-disabled");
    expect(screen.queryByRole("link", { name: /Open in/ })).toBeNull();
  });
});
