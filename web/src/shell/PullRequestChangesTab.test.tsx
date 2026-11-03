import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Tabs } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type { PullRequestChangedFile } from "@/hooks/usePullRequests";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import { PullRequestChangesTab } from "./PullRequestChangesTab";
import { useChangesViewState } from "./useChangesViewState";

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
}: {
  conversationId: string;
  files?: PullRequestChangedFile[];
  patch?: string;
  threads?: ReviewThread[];
}) {
  const prUrl = prUrlFor(conversationId);
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
      selected: true,
      changes: { data: { available: true, data: files } },
      prDiff: { data: { head_sha: "head", base_sha: "base", patch: current }, isLoading: false },
      reviews: {
        page: { data: threads, head_sha: "head", base_sha: "base" },
        isFetching: false,
        loadComments: () => {},
        loadingComments: null,
      },
    }) as unknown as React.ComponentProps<typeof PullRequestChangesTab>;
  const element = (current: string) => (
    <TooltipProvider>
      <Tabs value="changes">
        <PullRequestChangesTab {...tabProps(current)} />
      </Tabs>
    </TooltipProvider>
  );
  const result = render(element(patch));
  return { rerender: (next: string) => result.rerender(element(next)) };
}

const findInput = () => screen.getByRole("textbox", { name: "Find in changes" });
function openFind(query: string) {
  fireEvent.click(screen.getByRole("button", { name: "Find in changes" }));
  fireEvent.change(findInput(), { target: { value: query } });
}
const diffOf = (path: string) => document.querySelector<HTMLElement>(`[data-path="${path}"]`);
const rowOf = (path: string, line: number) =>
  diffOf(path)?.querySelector<HTMLElement>(`[data-line="${line}"]`) ?? null;
const sectionHeader = (path: string, expanded: boolean) =>
  screen.getByRole("button", { name: new RegExp(path.replace(".", "\\.")), expanded });

describe("PullRequestChangesTab", () => {
  afterEach(() => {
    vi.restoreAllMocks();
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

  it("collapses a revealed file when the reader collapses it again", () => {
    renderTab({ conversationId: "find-recollapse" });
    fireEvent.click(sectionHeader("b.ts", true));
    openFind("needle");
    expect(diffOf("b.ts")).not.toBeNull();

    fireEvent.click(sectionHeader("b.ts", true));

    expect(diffOf("b.ts")).toBeNull();
    expect(sectionHeader("b.ts", false)).toBeInTheDocument();
  });

  it("handles Cmd+F only while focus is inside the Changes tab", () => {
    renderTab({ conversationId: "find-shortcut" });
    expect(fireEvent.keyDown(document.body, { key: "f", metaKey: true })).toBe(true);
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();

    const header = sectionHeader("a.ts", true);
    header.focus();
    expect(fireEvent.keyDown(header, { key: "f", ctrlKey: true })).toBe(false);
    expect(findInput()).toHaveFocus();
  });

  it("keeps the query and the nearest match when the patch refreshes", () => {
    const { rerender } = renderTab({ conversationId: "find-refresh" });
    openFind("needle");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();

    rerender(A + addedFile("b.ts", ["new top", "gamma", "needle here"]) + C);

    expect(findInput()).toHaveValue("needle");
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    expect(rowOf("b.ts", 3)).not.toBeNull();
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

  it("closes on Escape", () => {
    renderTab({ conversationId: "find-escape" });
    openFind("needle");
    fireEvent.keyDown(findInput(), { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Find in changes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Find in changes" })).toBeInTheDocument();
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
