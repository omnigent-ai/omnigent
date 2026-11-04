import { fireEvent, render, screen } from "@testing-library/react";
import type { FileDiffMetadata } from "@pierre/diffs";
import { describe, expect, it, vi } from "vitest";
import type * as UsePullRequestsModule from "@/hooks/usePullRequests";
import type { PullRequestChangedFile } from "@/hooks/usePullRequests";
import { LARGE_DIFF_THRESHOLD, PullRequestFileSection } from "./PullRequestFileSection";

vi.mock("@/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof UsePullRequestsModule>()),
  usePullRequestReviewThreadComposer: () => ({
    draft: { pending: false, anchor: null },
    stale: false,
    selectLines: vi.fn(),
    cancel: vi.fn(),
  }),
}));
vi.mock("@pierre/diffs/react", () => ({
  FileDiff: ({ selectedLines }: { selectedLines: unknown }) => (
    <div data-testid="diff" data-selected={JSON.stringify(selectedLines ?? null)} />
  ),
}));

const FILE: PullRequestChangedFile = {
  path: "src/app.ts",
  name: "app.ts",
  status: "modified",
  lines_added: 1,
  lines_removed: 0,
  bytes: null,
  modified_at: null,
};

function renderSection(
  props: Partial<React.ComponentProps<typeof PullRequestFileSection>> = {},
  unifiedLineCount = 3,
) {
  const fileDiff = { name: FILE.path, type: "change", unifiedLineCount } as FileDiffMetadata;
  const onToggleCollapsed = vi.fn();
  const element = (overrides: typeof props) => (
    <PullRequestFileSection
      file={FILE}
      fileDiff={fileDiff}
      options={{}}
      registerRef={() => {}}
      collapsed={false}
      onToggleCollapsed={onToggleCollapsed}
      threads={[]}
      threadContext={{
        conversationId: "conv",
        prUrl: "pr",
        account: null,
        canReply: false,
        loadComments: () => {},
        loadingComments: null,
      }}
      matchingRevision
      {...overrides}
    />
  );
  const result = render(element(props));
  return { onToggleCollapsed, rerender: (next: typeof props) => result.rerender(element(next)) };
}

// The test setup's IntersectionObserver never fires, so sections stay unmounted
// unless something forces them.
describe("PullRequestFileSection overrides", () => {
  it("mounts the diff only when forced", () => {
    const { rerender } = renderSection();
    expect(screen.getByText("Loading diff…")).toBeInTheDocument();
    rerender({ forceMount: true });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("bypasses the large-diff gate", () => {
    const { rerender } = renderSection({ forceMount: true }, LARGE_DIFF_THRESHOLD + 1);
    expect(screen.getByRole("button", { name: "Show diff" })).toBeInTheDocument();
    rerender({ forceMount: true, bypassLargeDiffGate: true });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("shows a collapsed file while forced expanded", () => {
    const { rerender } = renderSection({ forceMount: true, collapsed: true });
    expect(screen.queryByTestId("diff")).toBeNull();
    rerender({ forceMount: true, collapsed: true, forceExpanded: true });
    expect(screen.getByRole("button", { name: /app\.ts/, expanded: true })).toBeInTheDocument();
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("forwards a highlight as the selected line and drops it again", () => {
    const { rerender } = renderSection({
      forceMount: true,
      highlight: { side: "deletions", line: 4 },
    });
    expect(JSON.parse(screen.getByTestId("diff").dataset.selected!)).toEqual({
      start: 4,
      end: 4,
      side: "deletions",
    });
    rerender({ forceMount: true, highlight: null });
    expect(JSON.parse(screen.getByTestId("diff").dataset.selected!)).toBeNull();
  });

  it("renders header actions outside the collapse toggle", () => {
    const onAction = vi.fn();
    const { onToggleCollapsed } = renderSection({
      headerActions: (
        <button type="button" onClick={onAction}>
          Copy path
        </button>
      ),
    });
    fireEvent.click(screen.getByRole("button", { name: "Copy path" }));
    expect(onAction).toHaveBeenCalledOnce();
    expect(onToggleCollapsed).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /app\.ts/ })).not.toContainElement(
      screen.getByRole("button", { name: "Copy path" }),
    );
  });
});
