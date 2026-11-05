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
import type { Comment } from "@/hooks/useComments";
import {
  requestLocalReviewNavigation,
  consumeLocalReviewNavigation,
  useLocalReviewNavigation,
} from "@/lib/localReviewContext";
import { viewedStorageKey, writeViewed, type PullRequestIdentity } from "@/lib/pullRequestViewed";
import type * as SonnerModule from "sonner";
import { PullRequestChangesTab } from "./PullRequestChangesTab";
import { useChangesViewState } from "./useChangesViewState";

const toast = vi.hoisted(() =>
  Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() }),
);
const mocks = vi.hoisted(() => ({ copyText: vi.fn(async () => {}), mobile: false }));
const comments = vi.hoisted(() => ({ data: [] as Comment[], isSuccess: true }));
vi.mock("@/hooks/useComments", () => ({
  useComments: () => ({ ...comments, isFetching: false }),
}));
vi.mock("./LocalReviewComment", () => ({
  LocalReviewComment: ({ comment }: { comment: Comment }) => (
    <section data-local-review-comment={comment.id} tabIndex={-1}>
      {comment.body}
    </section>
  ),
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
  usePullRequestReviewThreadComposer: () => ({
    draft: { pending: false, anchor: null },
    stale: false,
    selectLines: vi.fn(),
    cancel: vi.fn(),
  }),
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
  loading = false,
  selected = true,
}: {
  conversationId: string;
  files?: PullRequestChangedFile[];
  patch?: string;
  threads?: ReviewThread[];
  /** Changed-file list fields beyond the file array, such as truncation. */
  listing?: { has_more?: boolean; total_count?: number | null };
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
        capabilities: {},
        auth: { selected_account: null },
      },
      pr: { url: prUrl, head_sha: "head", base_sha: "base", state: "OPEN" },
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
  afterEach(() => {
    comments.data = [];
    comments.isSuccess = true;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it.each([true, false])(
    "waits for the patch, tab mount, and accepted focus (initially selected: %s)",
    async (selected) => {
      const conversationId = "local-comment-loading";
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
          head_sha: "head",
          base_sha: "base",
        },
      } as Comment;
      comments.data = [comment];
      vi.spyOn(Element.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
      const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
      const navigation = renderHook(() => useLocalReviewNavigation(conversationId));
      const tab = renderTab({ conversationId, patch: "", loading: true, selected });
      vi.useFakeTimers();
      try {
        act(() => requestLocalReviewNavigation(comment));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
        expect(navigation.result.current).toBe(comment);
        expect(scroll).not.toHaveBeenCalled();
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
        expect(diffOf("b.ts")?.querySelector("[data-local-review-comment]")).toHaveFocus(),
      );
      await waitFor(() => expect(navigation.result.current).toBeUndefined());
      expect(focus.mock.calls.length).toBeGreaterThan(1);
      consumeLocalReviewNavigation(conversationId);
    },
  );

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
}) {
  const props = {
    conversationId,
    data: {
      selected_pr_url: PR_URL,
      base_ref: "main",
      capabilities: {},
      auth: { selected_account: null },
    },
    pr: { url: PR_URL, number: 1, head_sha: head, base_sha: "base", state: "OPEN" },
    prUrl: PR_URL,
    canEdit: false,
    selected: true,
    changes: { data: { available: true, data: files, has_more: hasMore, head_sha: listHead } },
    prDiff: { data: { head_sha: diffHead, base_sha: "base" }, isLoading: false },
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

  it("resets viewed marks on a new head and shows a dismissible notice", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-head", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));

    rerender(viewedTab({ conversationId: "viewed-head", head: "head2" }));
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(screen.getByText("0 / 2 files viewed")).toBeInTheDocument();
    expect(screen.getByText(/viewed marks were reset/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/viewed marks were reset/)).toBeNull();
    rerender(viewedTab({ conversationId: "viewed-head", head: "head2" }));
    expect(screen.queryByText(/viewed marks were reset/)).toBeNull();
  });

  it("hides viewed state when the list is not pinned to a head", () => {
    // A provider that ignores revision pinning can serve a newer patch than the polled head.
    render(viewedTab({ conversationId: "viewed-unpinned", listHead: null }));

    expect(screen.queryByRole("checkbox", { name: "Viewed a.ts" })).toBeNull();
    expect(screen.queryByText(/files viewed/)).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Hide viewed" })).toBeNull();
  });

  it("waits for the list and the patch to agree on a head", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-mismatch", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));

    // The list moved to head2 while the patch on screen is still head1.
    rerender(viewedTab({ conversationId: "viewed-mismatch", head: "head2", diffHead: "head1" }));
    expect(screen.queryByRole("checkbox", { name: "Viewed a.ts" })).toBeNull();
    expect(screen.queryByText(/viewed marks were reset/)).toBeNull();

    rerender(viewedTab({ conversationId: "viewed-mismatch", head: "head2" }));
    expect(viewedBox("a.ts")).not.toBeChecked();
    expect(screen.getByText(/viewed marks were reset/)).toBeInTheDocument();
  });

  it("clears the reset notice once a file is marked on the new head", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-new-mark", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));
    rerender(viewedTab({ conversationId: "viewed-new-mark", head: "head2" }));
    expect(screen.getByText(/viewed marks were reset/)).toBeInTheDocument();

    fireEvent.click(viewedBox("b.ts"));
    expect(screen.queryByText(/viewed marks were reset/)).toBeNull();
    expect(screen.getByText("1 / 2 files viewed")).toBeInTheDocument();
  });

  it("keeps viewed marks when a poll returns the same head", () => {
    const { rerender } = render(viewedTab({ conversationId: "viewed-same", head: "head1" }));
    fireEvent.click(viewedBox("a.ts"));

    rerender(viewedTab({ conversationId: "viewed-same", head: "head1" }));
    expect(viewedBox("a.ts")).toBeChecked();
    expect(screen.queryByText(/viewed marks were reset/)).toBeNull();
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

    // H2 has a saved mark on a.ts, as if another tab marked it. Loading H2 keeps a.ts
    // collapsed, and that collapse still belongs to the mark: a fresh H3 expands it again.
    writeViewed(viewedStorageKey(window.location.origin, "alice", IDENTITY, "head2"), ["a.ts"]);
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
    writeViewed(viewedStorageKey(origin, "alice", IDENTITY, "head1"), ["a.ts"]);
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
