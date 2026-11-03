import { fireEvent, render, screen } from "@testing-library/react";
import type { FileDiffMetadata } from "@pierre/diffs";
import { afterEach, describe, expect, it, vi } from "vitest";
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
// Like @pierre/diffs: rows live in an open shadow root and are rebuilt on every
// render, then onPostRender runs.
vi.mock("@pierre/diffs/react", async () => {
  const { createElement, useEffect, useRef } = await import("react");
  return {
    FileDiff: ({
      options,
      selectedLines,
    }: {
      options: { onPostRender?: (node: HTMLElement, instance: unknown, phase: string) => unknown };
      selectedLines: unknown;
    }) => {
      const host = useRef<HTMLElement>(null);
      useEffect(() => {
        const node = host.current!;
        const root = node.shadowRoot ?? node.attachShadow({ mode: "open" });
        root.innerHTML =
          '<div data-line="4" data-line-type="change-deletion"><span>const </span><span>old</span></div>' +
          '<div data-line="4" data-line-type="change-addition"><span>new</span></div>';
        options.onPostRender?.(node, {}, "post");
      });
      return createElement("diffs-container", {
        ref: host,
        "data-testid": "diff",
        "data-selected": JSON.stringify(selectedLines ?? null),
      });
    },
  };
});

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

class FakeHighlight {
  ranges: Range[];
  constructor(...ranges: Range[]) {
    this.ranges = ranges;
  }
}

// The test setup's IntersectionObserver never fires, so sections stay unmounted
// unless something forces them.
describe("PullRequestFileSection overrides", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("mounts the diff only when forced", () => {
    const { rerender } = renderSection();
    expect(screen.getByText("Loading diff…")).toBeInTheDocument();
    rerender({ forceMount: true });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("keeps a forced section mounted after the force flag clears", () => {
    const { rerender } = renderSection({ forceMount: true });
    rerender({ forceMount: false });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("bypasses the large-diff gate", () => {
    const { rerender } = renderSection({ forceMount: true }, LARGE_DIFF_THRESHOLD + 1);
    expect(screen.getByRole("button", { name: "Show diff" })).toBeInTheDocument();
    rerender({ forceMount: true, bypassLargeDiffGate: true });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("keeps a large diff ungated after the bypass flag clears", () => {
    const { rerender } = renderSection(
      { forceMount: true, bypassLargeDiffGate: true },
      LARGE_DIFF_THRESHOLD + 1,
    );
    rerender({ forceMount: true, bypassLargeDiffGate: false });
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("shows a collapsed file while forced expanded", () => {
    const { rerender } = renderSection({ forceMount: true, collapsed: true });
    expect(screen.queryByTestId("diff")).toBeNull();
    rerender({ forceMount: true, collapsed: true, forceExpanded: true });
    expect(screen.getByRole("button", { name: /app\.ts/, expanded: true })).toBeInTheDocument();
    expect(screen.getByTestId("diff")).toBeInTheDocument();
  });

  it("leaves the reader's line selection alone while a find match is shown", () => {
    renderSection({
      forceMount: true,
      highlight: { side: "deletions", line: 4, start: 6, end: 9 },
    });
    expect(screen.getByTestId("diff")).toHaveAttribute("data-selected", "null");
  });

  it("paints the active match inside the diff's shadow root and centres its row", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    const registry = new Map<string, FakeHighlight>();
    vi.stubGlobal("CSS", { highlights: registry });
    vi.stubGlobal("Highlight", FakeHighlight);
    const highlight = { side: "deletions" as const, line: 4, start: 6, end: 9 };
    const { rerender } = renderSection({ forceMount: true, highlight });

    const painted = registry.get("pr-find-active")!.ranges[0];
    expect(painted.toString()).toBe("old");
    expect(painted.startContainer.parentElement?.closest("[data-line]")).toHaveAttribute(
      "data-line-type",
      "change-deletion",
    );
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll).toHaveBeenCalledWith({ block: "center" });

    // A re-render replaces the rows; the range is repainted without scrolling again.
    rerender({ forceMount: true, highlight });
    expect(registry.get("pr-find-active")!.ranges[0].toString()).toBe("old");
    expect(scroll).toHaveBeenCalledTimes(1);

    rerender({ forceMount: true, highlight: null });
    expect(registry.has("pr-find-active")).toBe(false);
  });

  it("centres the row without the highlight API", () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    renderSection({
      forceMount: true,
      highlight: { side: "additions", line: 4, start: 0, end: 3 },
    });
    expect(scroll).toHaveBeenCalledWith({ block: "center" });
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
