import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CopyIcon, FolderOpenIcon } from "lucide-react";
import {
  forwardRef,
  Suspense,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type ReactNode,
  useState,
} from "react";
import type {
  CodeViewItem,
  CodeViewScrollTarget,
  DiffLineAnnotation,
  SelectedLineRange,
} from "@pierre/diffs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isCommandAvailable, usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  DiffReview,
  type DiffReviewProps,
  type DiffReviewSourceLayer,
  listedFileAt,
  MARK_ALL_VIEWED_LOADING_REASON,
} from "./DiffReview";
import type { DiffReviewFileAction } from "./diffReviewFileAction";
import { pathFromReviewItemId, reviewFile } from "./diffReviewModel";
import {
  clearOwnedFindHighlights,
  DIFF_FIND_HIGHLIGHTS,
  paintOwnedFindHighlights,
} from "./findHighlight";
import { type FakeHighlight, installFakeHighlights } from "@/test/fakeHighlights";
import {
  readOwnedFilesOnly,
  resetUnsavedOwnedFilesOnly,
  writeOwnedFilesOnly,
} from "./ownedFilesPreference";
import { computeFileDiff } from "./fileDiffComputation";
import type { ReviewRevealTarget, ReviewScrollAnchor } from "./reviewNavigation";
import type { DiffReviewContent } from "./reviewItems";
import { installResizableWidth } from "@/test/resizableWidth";
import { CHANGES_REVIEW_TREE_WIDTH, PULL_REQUEST_REVIEW_TREE_WIDTH } from "./reviewTreeWidth";
import { resetRememberedPaneWidthForTesting } from "@/hooks/useRememberedPaneWidth";
import { chooseSettingValue, expectSetting, settingIn } from "@/test/paletteCommands";
import { TooltipProvider } from "@/components/ui/tooltip";
import { findTreeRow, treeRowPaths } from "@/test/fileTree";
import type { DiffReviewToolbarProps } from "./DiffReviewToolbar";

const device = vi.hoisted(() => ({ mobile: false }));
// A rich diff choice shows after its click; a test can hold that until it releases it.
const presentation = vi.hoisted(() => ({ held: false, queue: [] as (() => void)[] }));
vi.mock("./deferPresentation", async () => {
  const { startTransition } = await vi.importActual<{
    startTransition: (callback: () => void) => void;
  }>("react");
  return {
    deferPresentation: (apply: () => void) =>
      presentation.held ? presentation.queue.push(apply) : startTransition(apply),
  };
});
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => device.mobile }));

// CodeView is the library boundary: record what the review asks it to scroll to.
// The props it was last rendered with stand in for what a source layer reaches.
interface RecordedCodeViewProps {
  items: readonly CodeViewItem<unknown>[];
  options: Record<string, unknown>;
  selectedLines: { id: string; range: SelectedLineRange } | null;
  renderAnnotation?: (annotation: unknown, item: CodeViewItem<unknown>) => ReactNode;
  renderGutterUtility?: (hovered: () => unknown, item: CodeViewItem<unknown>) => ReactNode;
  renderHeaderMetadata?: (item: CodeViewItem<unknown>) => ReactNode;
  renderHeaderPrefix?: (item: CodeViewItem<unknown>) => ReactNode;
  renderHeaderFilenameSuffix?: (item: CodeViewItem<unknown>) => ReactNode;
  renderCodeViewFooter?: () => ReactNode;
  onScroll?: (scrollTop: number, viewer: unknown, details?: { engineOwned: boolean }) => void;
}
const codeView = vi.hoisted(() => ({
  scrolls: [] as CodeViewScrollTarget[],
  items: [] as readonly CodeViewItem<unknown>[],
  props: undefined as RecordedCodeViewProps | undefined,
  // While set, a render given a rich file's item waits on it, as a slow rich render would.
  richGate: undefined as Promise<void> | undefined,
  // Whether each committed render showed a rich file.
  commits: [] as boolean[],
}));
vi.mock("@pierre/diffs/react", () => ({
  WorkerPoolContextProvider: ({ children }: { children: ReactNode }) => children,
  CodeView: forwardRef(function CodeView(viewProps: RecordedCodeViewProps, ref) {
    const showsRich = viewProps.items.some((item) => item.type === "file");
    if (showsRich && codeView.richGate) throw codeView.richGate;
    useLayoutEffect(() => {
      codeView.commits.push(showsRich);
    });
    const container = useRef<HTMLDivElement>(null);
    codeView.items = viewProps.items;
    codeView.props = viewProps;
    // One instance per viewer, as the real one keeps; scroll listeners hear it with each scroll.
    const [instance] = useState(() => ({
      getContainerElement: () => container.current ?? undefined,
      getScrollTop: () => 0,
      getRenderedItems: () =>
        codeView.items
          .filter((item) => !item.collapsed)
          .map((item) => ({
            id: item.id,
            type: "diff",
            element: renderedHost(item.id),
            item,
            instance: { isLineRenderable: () => true, revealLine: () => undefined },
          })),
    }));
    useImperativeHandle(ref, () => ({
      scrollTo: (target: CodeViewScrollTarget) => codeView.scrolls.push(target),
      getItem: (id: string) => codeView.items.find((item) => item.id === id),
      getInstance: () => instance,
    }));
    return (
      <div
        ref={container}
        data-testid="code-view"
        onScroll={() => viewProps.onScroll?.(container.current?.scrollTop ?? 0, instance)}
      >
        {viewProps.renderCodeViewFooter?.()}
      </div>
    );
  }),
}));

function renderedHost(itemId = ""): HTMLElement {
  const host = document.createElement("div");
  host.dataset.reviewItem = itemId;
  host.attachShadow({ mode: "open" }).innerHTML =
    '<div data-content><div data-line="150" data-line-type="context">line 150</div></div>';
  return host;
}

const PATH = "src/long.ts";
const before = Array.from({ length: 300 }, (_, index) => `line ${index + 1}`).join("\n") + "\n";
const ready: DiffReviewContent = {
  status: "ready",
  fileDiff: computeFileDiff({
    path: PATH,
    before,
    after: before.replace("line 295\n", "line 295 changed\n"),
    ignoreWhitespace: false,
    revision: "1",
  }),
};
const loading: DiffReviewContent = { status: "loading" };

/** A ready one-line diff for `path`. */
function contentFor(path: string, after = "new\n"): DiffReviewContent {
  return {
    status: "ready",
    fileDiff: computeFileDiff({
      path,
      before: "old\n",
      after,
      ignoreWhitespace: false,
      revision: "1",
    }),
  };
}

function props<A = undefined>(
  content: DiffReviewContent,
  overrides: Partial<DiffReviewProps<A>> = {},
) {
  return {
    diagnosticsView: "working_copy" as const,
    files: [reviewFile(PATH, "modified", { additions: 1, deletions: 1 })],
    contents: new Map([[PATH, content]]),
    onRetry: () => undefined,
    revealPath: PATH,
    layout: "unified" as const,
    wrapLines: false,
    findOpen: false,
    findFocusRequest: 0,
    onFindClose: () => undefined,
    emptyMessage: "No changes",
    ...overrides,
  };
}

/** Lets the review's after-layout callbacks run. */
async function settle() {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
  });
}

/** The review's toolbar commands in ⌘K, apart from those for its current file. */
function toolbarCommandGroups() {
  return Object.entries(usePaletteCommandsStore.getState().groups)
    .filter(([source]) => !/^diff-review-(viewed|markdown|file|folder)/.test(source))
    .map(([, group]) => group);
}

/** Opens the toolbar's Diff options menu, as a pointer press does. */
function openDiffOptions(scope: HTMLElement = document.body) {
  fireEvent.pointerDown(within(scope).getByRole("button", { name: "Diff options" }), {
    button: 0,
  });
  return screen.getByRole("menu", { name: "Diff options" });
}

const lineScrolls = () => codeView.scrolls.filter((target) => target.type === "line");

// Every case starts from the same state, whatever order the cases run in.
afterEach(() => {
  codeView.scrolls = [];
  codeView.commits = [];
  codeView.items = [];
  codeView.props = undefined;
  codeView.richGate = undefined;
  device.mobile = false;
  presentation.held = false;
  presentation.queue = [];
  usePaletteCommandsStore.setState({ groups: {} });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  // Tree widths, viewed files and rich choices persist; a case's choices aren't the next one's.
  window.localStorage.clear();
  resetRememberedPaneWidthForTesting(CHANGES_REVIEW_TREE_WIDTH);
  resetRememberedPaneWidthForTesting(PULL_REQUEST_REVIEW_TREE_WIDTH);
});

describe("DiffReview placement", () => {
  it("reveals a cited line once its file's diff arrives", async () => {
    const target: ReviewRevealTarget = { path: PATH, line: 150, align: "center" };
    const view = render(<DiffReview {...props(loading, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toEqual([]);
    view.rerender(<DiffReview {...props(ready, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toEqual([expect.objectContaining({ lineNumber: 150, align: "center" })]);
  });

  it("drops a citation the reader left while its file was loading", async () => {
    let pending = true;
    const target: ReviewRevealTarget = {
      path: PATH,
      line: 150,
      align: "center",
      persistent: () => pending,
    };
    const view = render(<DiffReview {...props(loading, { revealTarget: target })} />);
    await settle();
    pending = false;
    view.rerender(<DiffReview {...props(ready, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toEqual([]);
  });

  it("drops a pending reveal when the reader scrolls the review", async () => {
    const target: ReviewRevealTarget = { path: PATH, line: 150, align: "center" };
    const view = render(<DiffReview {...props(loading, { revealTarget: target })} />);
    await settle();
    fireEvent.wheel(screen.getByTestId("code-view"));
    view.rerender(<DiffReview {...props(ready, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toEqual([]);
  });

  it("restores the reader's place by its line, which brings hidden context back", async () => {
    const anchor: ReviewScrollAnchor = { path: PATH, line: 150, offset: 0 };
    const view = render(<DiffReview {...props(loading, { initialScrollAnchor: anchor })} />);
    await settle();
    view.rerender(<DiffReview {...props(ready, { initialScrollAnchor: anchor })} />);
    await settle();
    expect(lineScrolls()).toEqual([
      expect.objectContaining({ lineNumber: 150, side: "additions", align: "start" }),
    ]);
  });

  it("doesn't restore a place the reader scrolled away from while it loaded", async () => {
    const anchor: ReviewScrollAnchor = { path: PATH, line: 150, offset: 0 };
    const view = render(<DiffReview {...props(loading, { initialScrollAnchor: anchor })} />);
    await settle();
    fireEvent.keyDown(screen.getByTestId("code-view"), { key: "PageDown" });
    const scrollsBefore = codeView.scrolls.length;
    view.rerender(<DiffReview {...props(ready, { initialScrollAnchor: anchor })} />);
    await settle();
    expect(codeView.scrolls.slice(scrollsBefore)).toEqual([]);
  });

  it.each([
    ["while its diff loads", true],
    ["once the diffs have arrived", false],
  ])(
    "takes a newer file request over the saved place it opened on (%s)",
    async (_when, beforeDiffs) => {
      const anchor: ReviewScrollAnchor = { path: PATH, line: 150, offset: 0 };
      const OTHER = "src/new-address.ts";
      const files = [
        reviewFile(PATH, "modified", { additions: 1, deletions: 1 }),
        reviewFile(OTHER, "modified", { additions: 1, deletions: 1 }),
      ];
      const review = (arrived: boolean, revealPath: string | null) => (
        <DiffReview
          {...props(arrived ? ready : loading, { initialScrollAnchor: anchor, revealPath })}
          files={files}
          contents={
            new Map([
              [PATH, arrived ? ready : loading],
              [OTHER, arrived ? contentFor(OTHER) : loading],
            ])
          }
        />
      );
      // Opened on no file, so the saved place is pending; then Back or Forward names a file.
      const view = render(review(false, null));
      await settle();
      if (!beforeDiffs) {
        view.rerender(review(true, null));
        await settle();
        codeView.scrolls.length = 0;
      }
      view.rerender(review(!beforeDiffs, OTHER));
      await settle();
      view.rerender(review(true, OTHER));
      await settle();
      expect(lineScrolls().filter((target) => target.id === `diff:${PATH}`)).toEqual([]);
      expect(codeView.scrolls).toContainEqual(expect.objectContaining({ id: `diff:${OTHER}` }));
    },
  );

  it("reveals the opened file again after a visit without one, as Back and Forward do", async () => {
    const view = render(<DiffReview {...props(ready)} />);
    await settle();
    expect(codeView.scrolls, "first visit").not.toEqual([]);
    view.rerender(<DiffReview {...props(ready, { revealPath: null })} />);
    await settle();
    codeView.scrolls.length = 0;
    view.rerender(<DiffReview {...props(ready)} />);
    await settle();
    expect(codeView.scrolls, "return").not.toEqual([]);
  });

  it("doesn't return to a retired citation when its file refreshes", async () => {
    const target: ReviewRevealTarget = {
      path: PATH,
      line: 150,
      align: "center",
      persistent: () => true,
    };
    const other = reviewFile("src/other.ts", "added", { additions: 1 });
    const withOther = (content: DiffReviewContent, overrides: Partial<DiffReviewProps>) => ({
      ...props(content, overrides),
      files: [reviewFile(PATH, "modified", { additions: 1, deletions: 1 }), other],
    });
    const view = render(<DiffReview {...withOther(ready, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toHaveLength(1);
    view.rerender(
      <DiffReview {...withOther(ready, { revealPath: other.path, revealTarget: null })} />,
    );
    await settle();
    const refreshed: DiffReviewContent = { ...ready };
    view.rerender(
      <DiffReview {...withOther(refreshed, { revealPath: other.path, revealTarget: null })} />,
    );
    await settle();
    expect(lineScrolls()).toHaveLength(1);
  });
});

describe("DiffReview layout", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("shows a split review unified while it's too narrow, and split again once it widens", () => {
    const resize = installResizableWidth(600);
    render(<DiffReview {...props(ready, { layout: "split" })} />);
    const diffStyle = () => codeView.props!.options.diffStyle;
    expect(diffStyle()).toBe("unified");
    resize(720);
    expect(diffStyle()).toBe("split");
    resize(719);
    expect(diffStyle()).toBe("unified");
  });
});

describe("DiffReview toolbar", () => {
  afterEach(() => {
    usePaletteCommandsStore.setState({ groups: {} });
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("names the base it compares against beside the file count, only when given one", () => {
    const base = { label: "vs HEAD", description: "Compared with the last commit." };
    const view = render(<DiffReview {...props(ready, { base })} />);
    const label = screen.getByTestId("diff-review-base");
    expect(label).toHaveTextContent("vs HEAD");
    expect(label).toHaveAttribute("aria-description", "Compared with the last commit.");
    view.rerender(<DiffReview {...props(ready, { base: null })} />);
    expect(screen.queryByTestId("diff-review-base")).toBeNull();
  });

  it("changes layout, wrapping, and whitespace through the source's setters", () => {
    const onLayoutChange = vi.fn();
    const onWrapLinesChange = vi.fn();
    const onIgnoreWhitespaceChange = vi.fn();
    render(
      <DiffReview
        {...props(ready, { onLayoutChange, onWrapLinesChange, onIgnoreWhitespaceChange })}
      />,
    );
    const toolbar = screen.getByRole("toolbar", { name: "Diff review" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Switch to split view" }));
    // Wrapping sits in the Diff options menu, not as a toolbar button.
    expect(within(toolbar).queryByRole("button", { name: "Wrap lines" })).toBeNull();
    openDiffOptions(toolbar);
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Wrap long lines" }));
    fireEvent.click(within(toolbar).getByRole("button", { name: "Hide whitespace changes" }));
    expect(onLayoutChange).toHaveBeenCalledWith("split");
    expect(onWrapLinesChange).toHaveBeenCalledWith(true);
    expect(onIgnoreWhitespaceChange).toHaveBeenCalledWith(true);
  });

  it("shows each setting's state as a pressed button", () => {
    render(
      <DiffReview
        {...props(ready, {
          layout: "split",
          wrapLines: true,
          ignoreWhitespace: true,
          onLayoutChange: () => undefined,
          onWrapLinesChange: () => undefined,
          onIgnoreWhitespaceChange: () => undefined,
        })}
      />,
    );
    for (const name of ["Switch to unified view", "Show whitespace changes"]) {
      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-pressed", "true");
    }
    openDiffOptions();
    expect(screen.getByRole("menuitemcheckbox", { name: "Wrap long lines" })).toBeChecked();
  });

  it("hides the layout choice while too narrow to split, keeping the saved layout", () => {
    const resize = installResizableWidth(600);
    const onLayoutChange = vi.fn();
    render(<DiffReview {...props(ready, { layout: "split", onLayoutChange })} />);
    const toggle = () => screen.queryByRole("button", { name: "Switch to unified view" });
    expect(toggle()).toBeNull();
    resize(900);
    expect(toggle()).toBeInTheDocument();
    resize(600);
    expect(toggle()).toBeNull();
    expect(onLayoutChange).not.toHaveBeenCalled();
  });

  it("gathers the view controls into one menu while narrow, keeping every action", () => {
    const resize = installResizableWidth(300);
    const onWrapLinesChange = vi.fn();
    render(
      <DiffReview
        {...props(ready, { onWrapLinesChange, onIgnoreWhitespaceChange: () => undefined })}
      />,
    );
    const toolbar = screen.getByRole("toolbar", { name: "Diff review" });
    expect(within(toolbar).queryByRole("button", { name: "Wrap lines" })).toBeNull();
    fireEvent.pointerDown(within(toolbar).getByRole("button", { name: "More view options" }), {
      button: 0,
    });
    const menu = screen.getByRole("menu");
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Skip test files", "Hide whitespace changes", "Collapse all files"]);
    // A toggle keeps the menu open and shows the setting's new action.
    fireEvent.click(within(menu).getByRole("menuitem", { name: "Hide whitespace changes" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    // Wrapping and sorting keep their own menu while narrow.
    openDiffOptions(toolbar);
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Wrap long lines" }));
    expect(onWrapLinesChange).toHaveBeenCalledWith(true);

    resize(900);
    expect(within(toolbar).queryByRole("button", { name: "More view options" })).toBeNull();
    expect(within(toolbar).getByRole("button", { name: "Diff options" })).toBeInTheDocument();
  });

  it("sorts the diff by size from its options, as every review then does", () => {
    const sized = [
      reviewFile("a.ts", "modified", { additions: 1 }),
      reviewFile("big/b.ts", "modified", { additions: 12, deletions: 8 }),
      reviewFile("c.ts", "modified", { additions: 5 }),
    ];
    const onWrapLinesChange = vi.fn();
    const sizedProps = props(ready, {
      files: sized,
      contents: new Map(sized.map((file) => [file.path, contentFor(file.path)])),
      revealPath: null,
      onWrapLinesChange,
    });
    const shownOrder = () =>
      codeView.items.filter((item) => item.id.startsWith("diff:")).map((item) => item.id);
    const view = render(<DiffReview {...sizedProps} />);
    expect(shownOrder()).toEqual(["diff:big/b.ts", "diff:a.ts", "diff:c.ts"]);

    // The menu is named after its button, so a screen reader says what opened.
    expect(openDiffOptions()).toBeVisible();
    expect(screen.getByRole("menuitemradio", { name: "By path" })).toBeChecked();
    fireEvent.click(screen.getByRole("menuitemradio", { name: "By size, largest first" }));
    expect(shownOrder()).toEqual(["diff:big/b.ts", "diff:c.ts", "diff:a.ts"]);
    // Wrapping is untouched by sorting.
    openDiffOptions();
    expect(screen.getByRole("menuitemcheckbox", { name: "Wrap long lines" })).not.toBeChecked();
    expect(onWrapLinesChange).not.toHaveBeenCalled();

    // Another review opened later starts in the order chosen here.
    view.unmount();
    render(<DiffReview {...sizedProps} />);
    expect(shownOrder()).toEqual(["diff:big/b.ts", "diff:c.ts", "diff:a.ts"]);
  });

  it("sorts by size from ⌘K's setting, through the popover's own command", () => {
    const sized = [
      reviewFile("a.ts", "modified", { additions: 1 }),
      reviewFile("z.ts", "modified", { additions: 9 }),
    ];
    render(
      <DiffReview
        {...props(ready, {
          files: sized,
          contents: new Map(sized.map((file) => [file.path, contentFor(file.path)])),
          revealPath: null,
        })}
      />,
    );
    const commands = toolbarCommandGroups()[0]!.commands;
    expectSetting(commands, "Sort changed files", {
      current: "By path",
      choices: ["By path", "By size, largest first"],
    });
    act(
      () =>
        void chooseSettingValue(
          settingIn(commands, "Sort changed files"),
          "By size, largest first",
        ),
    );
    expect(
      codeView.items.filter((item) => item.id.startsWith("diff:")).map((item) => item.id),
    ).toEqual(["diff:z.ts", "diff:a.ts"]);
    openDiffOptions();
    expect(screen.getByRole("menuitemradio", { name: "By size, largest first" })).toBeChecked();
  });

  it("leaves out the controls a source doesn't offer", () => {
    render(<DiffReview {...props(ready)} />);
    expect(screen.queryByRole("button", { name: "Switch to split view" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Wrap lines" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Hide whitespace changes" })).toBeNull();
  });

  it("offers the same commands in ⌘K under the source's heading, and withdraws them on unmount", () => {
    const onWrapLinesChange = vi.fn();
    const view = render(
      <DiffReview {...props(ready, { onWrapLinesChange, commandHeading: "Changes" })} />,
    );
    const groups = toolbarCommandGroups;
    expect(groups().map((group) => group.heading)).toEqual(["Changes"]);
    const labels = groups()[0]!.commands.map((command) => command.label);
    expect(labels).toEqual([
      "Hide changed files",
      "Search changed files",
      "Filter changed files",
      "Show changes by language",
      "Show hidden matches",
      "Test files",
      "Wrap long lines",
      "Sort changed files",
      "Collapse all files",
    ]);
    // The view settings show their value, as the toolbar's pressed controls do.
    expectSetting(groups()[0]!.commands, "Test files", {
      current: "All files",
      choices: ["All files", "Skip test files", "Only test files"],
    });
    expectSetting(groups()[0]!.commands, "Wrap long lines", {
      current: "Off",
      choices: ["On", "Off"],
    });
    act(() => void chooseSettingValue(settingIn(groups()[0]!.commands, "Wrap long lines"), "On"));
    expect(onWrapLinesChange).toHaveBeenCalledWith(true);
    view.unmount();
    expect(groups()).toEqual([]);
  });

  it("withdraws its commands while hidden and offers them again once shown", () => {
    const view = render(<DiffReview {...props(ready, { commandsActive: false })} />);
    const groups = toolbarCommandGroups;
    expect(groups()).toEqual([]);
    view.rerender(<DiffReview {...props(ready, { commandsActive: true })} />);
    expect(groups()).toHaveLength(1);
    view.rerender(<DiffReview {...props(ready, { commandsActive: false })} />);
    expect(groups()).toEqual([]);
  });

  it("opens the file search from ⌘K, showing the tree if it was hidden", () => {
    render(<DiffReview {...props(ready)} />);
    const command = (label: string) =>
      toolbarCommandGroups()[0]!.commands.find((candidate) => candidate.label === label)!;
    act(() => command("Hide changed files").run());
    expect(screen.queryByRole("complementary", { name: "Changed files" })).toBeNull();
    act(() => command("Search changed files").run());
    expect(screen.getByRole("textbox", { name: "Filter changed files by name" })).toHaveFocus();
  });
});

describe("DiffReview changed-files tree", () => {
  afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

  const command = (label: string) =>
    toolbarCommandGroups()[0]!.commands.find((candidate) => candidate.label === label)!;
  const searchBox = () => screen.getByRole("textbox", { name: "Filter changed files by name" });
  const treeText = () =>
    screen.getByRole("complementary", { name: "Changed files" }).querySelector("[data-file-tree]")
      ?.textContent ?? "";

  it("focuses an open file search again when it's asked for again", () => {
    render(<DiffReview {...props(ready)} />);
    act(() => command("Search changed files").run());
    expect(searchBox()).toHaveFocus();
    act(() => screen.getByTestId("diff-review-file-count").focus());
    act(() => screen.getByRole("button", { name: "Filter changed files" }).focus());
    expect(searchBox()).not.toHaveFocus();
    act(() => command("Search changed files").run());
    expect(searchBox()).toHaveFocus();
  });

  it("docks and undocks the column from a pressed toolbar toggle, with no popover", () => {
    render(<DiffReview {...props(ready)} />);
    const toggle = screen.getByRole("button", { name: "Hide changed files" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(toggle).not.toHaveAttribute("aria-haspopup");
    expect(toggle).not.toHaveAttribute("aria-expanded");
    expect(toggle).not.toHaveAttribute("aria-controls");

    fireEvent.click(toggle);
    expect(screen.queryByRole("complementary", { name: "Changed files" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Changed files" })).toBeNull();
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(screen.getByRole("complementary", { name: "Changed files" })).toBeVisible();
  });

  it("offers no close button as a column, where its × only clears the search", () => {
    render(<DiffReview {...props(ready)} />);
    act(() => command("Search changed files").run());
    const column = screen.getByRole("complementary", { name: "Changed files" });
    expect(within(column).queryByRole("button", { name: "Close changed files" })).toBeNull();
    fireEvent.click(within(column).getByRole("button", { name: "Close changed-file search" }));
    expect(screen.queryByRole("textbox", { name: "Filter changed files by name" })).toBeNull();
    expect(screen.getByRole("complementary", { name: "Changed files" })).toBeVisible();
  });

  it("keeps the tree filtered by the search after a file is picked", async () => {
    const files = [reviewFile("src/alpha.ts", "modified"), reviewFile("src/beta.ts", "modified")];
    render(<DiffReview {...props(ready, { files })} />);
    act(() => command("Search changed files").run());
    fireEvent.change(searchBox(), { target: { value: "alpha" } });
    await settle();
    expect(treeRowPaths()).toEqual(["src/", "src/alpha.ts"]);

    const row = await findTreeRow("src/alpha.ts");
    act(() => row.click());
    await settle();
    expect(searchBox()).toHaveValue("alpha");
    expect(treeRowPaths()).toEqual(["src/", "src/alpha.ts"]);
  });

  describe("as a popover while the review is narrow", () => {
    afterEach(() => {
      device.mobile = false;
      vi.unstubAllGlobals();
    });
    const files = [reviewFile("src/alpha.ts", "modified"), reviewFile("src/beta.ts", "modified")];
    const openPopoverAndPick = async () => {
      installResizableWidth(600);
      render(<DiffReview {...props(ready, { files })} />);
      act(() => command("Show changed files").run());
      const row = await findTreeRow("src/beta.ts");
      act(() => row.click());
      await settle();
    };

    it("stays open after a pick as an anchored popover, ready for the next file", async () => {
      await openPopoverAndPick();
      const popover = screen.getByRole("dialog", { name: "Changed files" });
      expect(popover).toHaveAttribute("data-layout", "anchored");
      expect(popover).toHaveClass("w-72");
      expect(treeRowPaths()).toEqual(["src/", "src/alpha.ts", "src/beta.ts"]);
    });

    it("tells its source which file the reader picked in the tree", async () => {
      installResizableWidth(600);
      const chosen: string[] = [];
      render(
        <DiffReview {...props(ready, { files, onTreeFileChosen: (path) => chosen.push(path) })} />,
      );
      act(() => command("Show changed files").run());
      const row = await findTreeRow("src/beta.ts");
      act(() => row.click());
      await settle();
      expect(chosen).toEqual(["src/beta.ts"]);
    });

    it("opens from its toolbar toggle and hands focus back to it on Escape", async () => {
      installResizableWidth(600);
      render(<DiffReview {...props(ready, { files })} />);
      const toggle = screen.getByRole("button", { name: "Show changed files" });
      expect(toggle).not.toHaveAttribute("aria-pressed");
      expect(toggle).toHaveAttribute("aria-haspopup", "dialog");
      expect(toggle).toHaveAttribute("aria-expanded", "false");

      act(() => toggle.focus());
      await userEvent.keyboard("{Enter}");
      const popover = screen.getByRole("dialog", { name: "Changed files" });
      expect(toggle).toHaveAttribute("aria-expanded", "true");
      expect(toggle).toHaveAttribute("aria-controls", popover.id);
      expect(toggle).toHaveAccessibleName("Hide changed files");

      expect(popover).toContainElement(document.activeElement as HTMLElement);
      await userEvent.keyboard("{Escape}");
      await settle();
      expect(screen.queryByRole("dialog", { name: "Changed files" })).toBeNull();
      expect(toggle).toHaveFocus();
      expect(toggle).toHaveAttribute("aria-expanded", "false");
    });

    it("closes from its own × while a search keeps its separate toggle", () => {
      installResizableWidth(600);
      render(<DiffReview {...props(ready, { files })} />);
      act(() => command("Search changed files").run());
      const popover = screen.getByRole("dialog", { name: "Changed files" });
      expect(
        within(popover).getByRole("button", { name: "Close changed-file search" }),
      ).toBeVisible();
      fireEvent.click(within(popover).getByRole("button", { name: "Close changed files" }));
      expect(screen.queryByRole("dialog", { name: "Changed files" })).toBeNull();
    });

    it("spans the screen in the phone layout, and closes after a pick there", async () => {
      device.mobile = true;
      installResizableWidth(600);
      render(<DiffReview {...props(ready, { files })} />);
      act(() => command("Show changed files").run());
      const popover = screen.getByRole("dialog", { name: "Changed files" });
      expect(popover).toHaveAttribute("data-layout", "full-width");
      expect(popover).not.toHaveClass("w-72");
      const row = await findTreeRow("src/beta.ts");
      act(() => row.click());
      await settle();
      expect(screen.queryByRole("dialog", { name: "Changed files" })).toBeNull();
    });
  });

  it("updates a file's counts in place, keeping the search and its caret", () => {
    const counted = (additions: number) => [
      reviewFile(PATH, "modified", { additions, deletions: 1 }),
    ];
    const view = render(<DiffReview {...props(ready, { files: counted(1) })} />);
    act(() => command("Search changed files").run());
    const input = searchBox() as HTMLInputElement;
    fireEvent.change(input, { target: { value: "long" } });
    input.setSelectionRange(1, 1);
    expect(treeText()).toContain("+1");

    view.rerender(<DiffReview {...props(ready, { files: counted(2) })} />);
    expect(searchBox()).toBe(input);
    expect(input).toHaveValue("long");
    expect(input).toHaveFocus();
    expect(input.selectionStart).toBe(1);
    expect(treeText()).toContain("+2");
    expect(treeText()).not.toContain("+1");
  });
});

describe("DiffReview source layer", () => {
  interface Note {
    text: string;
  }
  const note: DiffLineAnnotation<Note> = {
    side: "deletions",
    lineNumber: 4,
    metadata: { text: "Why remove this?" },
  };
  const recorded = () => codeView.props!;
  const diffItem = () => recorded().items.find((item) => item.type === "diff")!;

  it("adds nothing to the diff without one", () => {
    render(<DiffReview {...props(ready)} />);
    const { options, renderAnnotation, renderGutterUtility } = recorded();
    expect(options).toMatchObject({ enableGutterUtility: false, enableLineSelection: true });
    expect(options).not.toHaveProperty("loadDiffFiles");
    expect(options).not.toHaveProperty("onLineSelectionEnd");
    expect(renderAnnotation).toBeUndefined();
    expect(renderGutterUtility).toBeUndefined();
    // The footer holds only the room that lets the last lines center.
    const endSpace = screen.getByTestId("diff-review-end-space");
    expect(screen.getByTestId("code-view").children).toHaveLength(1);
    expect(screen.getByTestId("code-view").firstElementChild).toBe(endSpace);
    expect(diffItem()).not.toHaveProperty("annotations");
  });

  it("shows a source's annotations, gutter control, and chrome for each file by path", async () => {
    const load = vi.fn(async () => ({
      oldFile: { name: PATH, contents: "" },
      newFile: { name: PATH, contents: "" },
    }));
    const renderAnnotation = vi.fn((annotation: DiffLineAnnotation<Note>, path: string) => (
      <span>
        {path}: {annotation.metadata.text}
      </span>
    ));
    const renderGutterUtility = vi.fn((_hovered: unknown, path: string) => <span>{path}</span>);
    const source: DiffReviewSourceLayer<Note> = {
      annotations: new Map([[PATH, [note]]]),
      renderAnnotation,
      renderGutterUtility,
      expandContext: { load, lineCount: 10 },
      renderHeaderExtras: (file) => <span>extras for {file.path}</span>,
      toolbar: <button type="button">Next conversation</button>,
      footer: <p>Earlier conversations</p>,
    };
    render(<DiffReview<Note> {...props<Note>(ready, { source })} />);

    expect(diffItem()).toMatchObject({ annotations: [note] });
    expect(recorded().options).toMatchObject({ enableGutterUtility: true, expansionLineCount: 10 });
    const loadDiffFiles = recorded().options.loadDiffFiles as (fileDiff: unknown) => unknown;
    await loadDiffFiles(ready.status === "ready" ? ready.fileDiff : undefined);
    expect(load).toHaveBeenCalledWith(ready.status === "ready" ? ready.fileDiff : undefined);
    render(<>{recorded().renderAnnotation!(note, diffItem())}</>);
    expect(screen.getByText(`${PATH}: Why remove this?`)).toBeInTheDocument();
    const hovered = () => ({ lineNumber: 4, side: "deletions" });
    recorded().renderGutterUtility!(hovered, diffItem());
    expect(renderGutterUtility).toHaveBeenCalledWith(hovered, PATH);
    render(<TooltipProvider>{recorded().renderHeaderMetadata!(diffItem())}</TooltipProvider>);
    expect(screen.getByText(`extras for ${PATH}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next conversation" })).toBeInTheDocument();
    expect(screen.getByText("Earlier conversations")).toBeInTheDocument();
    // The source's footer comes before the end space, so it stays reachable.
    expect(screen.getByText("Earlier conversations").nextElementSibling).toBe(
      screen.getByTestId("diff-review-end-space"),
    );
  });

  it("reports pointer selections by path, previewing until the pointer lets go", () => {
    const onLineSelection = vi.fn();
    render(<DiffReview {...props(ready, { source: { onLineSelection } })} />);
    const range: SelectedLineRange = { start: 3, end: 5, side: "deletions" };
    const context = { item: { id: diffItem().id } };
    const callback = (name: string) =>
      recorded().options[name] as (range: SelectedLineRange | null, context: unknown) => void;
    callback("onLineSelectionStart")(range, context);
    callback("onLineSelectionChange")(range, context);
    callback("onLineSelectionEnd")(range, context);
    callback("onLineSelectionEnd")(null, context);
    expect(onLineSelection.mock.calls).toEqual([
      [PATH, range, "preview"],
      [PATH, range, "preview"],
      [PATH, range, "commit"],
      [PATH, null, "commit"],
    ]);
  });

  it("selects the source's lines on either side in place of marked lines", () => {
    const markedLines = { path: PATH, start: 1, end: 2 };
    const range: SelectedLineRange = { start: 4, end: 4, side: "deletions" };
    const view = render(<DiffReview {...props(ready, { markedLines })} />);
    expect(recorded().selectedLines).toEqual({
      id: diffItem().id,
      range: { start: 1, end: 2, side: "additions" },
    });
    view.rerender(
      <DiffReview
        {...props(ready, { markedLines, source: { selectedLines: { path: PATH, range } } })}
      />,
    );
    expect(recorded().selectedLines).toEqual({ id: diffItem().id, range });
    view.rerender(
      <DiffReview {...props(ready, { markedLines, source: { selectedLines: null } })} />,
    );
    expect(recorded().selectedLines).toBeNull();
  });

  it("reveals a line of the old version on the deleted side", async () => {
    const target: ReviewRevealTarget = { path: PATH, line: 4, side: "deletions", align: "center" };
    render(<DiffReview {...props(ready, { revealTarget: target })} />);
    await settle();
    expect(lineScrolls()).toEqual([
      expect.objectContaining({ lineNumber: 4, side: "deletions", align: "center" }),
    ]);
  });
});

describe("DiffReview rich Markdown diff", () => {
  const MARKDOWN = "docs/guide.md";
  const markdownReady: DiffReviewContent = {
    status: "ready",
    fileDiff: computeFileDiff({
      path: MARKDOWN,
      before: "# Guide\n\nThe quick fox.\n",
      after: "# Guide\n\nThe slow fox.\n",
      ignoreWhitespace: false,
      revision: "1",
    }),
  };
  const markdownProps = (overrides: Partial<DiffReviewProps> = {}) =>
    props(markdownReady, {
      files: [reviewFile(MARKDOWN, "modified", { additions: 1, deletions: 1 })],
      contents: new Map([[MARKDOWN, markdownReady]]),
      revealPath: MARKDOWN,
      reviewScope: "test-review",
      ...overrides,
    });
  const item = () => codeView.props!.items[0]!;
  const header = () =>
    within(
      render(<TooltipProvider>{codeView.props!.renderHeaderMetadata!(item())}</TooltipProvider>)
        .container,
    );
  const clickRichToggle = () => {
    const toggle = header().getByRole("button", { name: `Rich diff of ${MARKDOWN}` });
    act(() => fireEvent.click(toggle));
  };

  afterEach(() => {
    window.localStorage.clear();
    usePaletteCommandsStore.setState({ groups: {} });
    presentation.held = false;
    presentation.queue = [];
  });
  /** Holds each choice's showing until `releasePresentation`. */
  const holdPresentation = () => {
    presentation.held = true;
  };
  const releasePresentation = () => {
    presentation.held = false;
    act(() => {
      for (const apply of presentation.queue.splice(0)) apply();
    });
  };
  const storedRich = () => window.localStorage.getItem("omnigent.diffReview.richMarkdownFiles");

  it("goes back to the source diff when switched twice before the rich diff shows", () => {
    render(<DiffReview {...markdownProps()} />);
    holdPresentation();
    clickRichToggle();
    // The click is done; the rich diff isn't showing yet, and the switch still says so.
    expect(item().type).toBe("diff");
    expect(header().getByRole("button", { name: `Rich diff of ${MARKDOWN}` })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    clickRichToggle();
    releasePresentation();
    expect(item().type).toBe("diff");
    expect(storedRich()).not.toContain(MARKDOWN);
  });

  it("withdraws a rich choice not yet shown for a line request, then reveals the line", async () => {
    const view = render(<DiffReview {...markdownProps()} />);
    holdPresentation();
    clickRichToggle();
    const target: ReviewRevealTarget = { path: MARKDOWN, line: 3, align: "center" };
    view.rerender(<DiffReview {...markdownProps({ revealTarget: target })} />);
    await settle();
    releasePresentation();
    await settle();
    expect(item().type).toBe("diff");
    expect(storedRich()).not.toContain(MARKDOWN);
    expect(lineScrolls()).toEqual([expect.objectContaining({ lineNumber: 3 })]);
  });

  it("never shows a rich choice a line request withdrew while React still held its render", async () => {
    let release = () => {};
    codeView.richGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const tree = (overrides: Partial<DiffReviewProps> = {}) => (
      <Suspense fallback={null}>
        <DiffReview {...markdownProps(overrides)} />
      </Suspense>
    );
    const view = render(tree());
    // A real transition: React holds the rich render, not a delayed callback.
    clickRichToggle();
    await act(async () => {
      await Promise.resolve();
    });
    expect(item().type).toBe("diff");
    const target: ReviewRevealTarget = { path: MARKDOWN, line: 3, align: "center" };
    view.rerender(tree({ revealTarget: target }));
    const afterRequest = codeView.commits.length;
    await act(async () => {
      codeView.richGate = undefined;
      release();
      await Promise.resolve();
    });
    await settle();
    expect(codeView.commits.slice(afterRequest)).not.toContain(true);
    expect(item().type).toBe("diff");
    expect(lineScrolls()).toEqual([expect.objectContaining({ lineNumber: 3 })]);
  });

  it("finds in what shows while a rich choice is pending, and in the preview once it shows", async () => {
    const view = render(<DiffReview {...markdownProps()} />);
    holdPresentation();
    clickRichToggle();
    view.rerender(<DiffReview {...markdownProps({ findOpen: true })} />);
    await settle();
    fireEvent.change(screen.getByTestId("diff-review-find-input"), { target: { value: "slow" } });
    await settle();
    // The source diff still shows: the match is its line.
    expect(lineScrolls().at(-1)).toEqual(expect.objectContaining({ lineNumber: 3 }));
    releasePresentation();
    await settle();
    expect(item().type).toBe("file");
  });

  it("switches a Markdown file to its rendered diff from the header and back", async () => {
    render(<DiffReview {...markdownProps()} />);
    expect(item().type).toBe("diff");
    const toggle = header().getByRole("button", { name: `Rich diff of ${MARKDOWN}` });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    act(() => fireEvent.click(toggle));
    expect(item().type).toBe("file");
    const annotation = (item() as { annotations: { lineNumber: number; metadata: unknown }[] })
      .annotations[0]!;
    expect(annotation.lineNumber).toBe(0);
    // The row asks for its preview once mounted; CodeView then shows the prepared one.
    const row = () => <>{codeView.props!.renderAnnotation!(annotation, item())}</>;
    const mounted = render(row());
    await settle();
    mounted.rerender(row());
    expect(await screen.findByText("slow", { selector: ".markdown-diff-word" })).toBeVisible();
    expect(screen.getByText("quick", { selector: ".markdown-diff-word" })).toBeVisible();
    expect(screen.getByRole("group", { name: "Added" })).toHaveTextContent("The slow fox.");
    const pressed = header().getByRole("button", { name: `Rich diff of ${MARKDOWN}` });
    expect(pressed).toHaveAttribute("aria-pressed", "true");
    act(() => fireEvent.click(pressed));
    expect(item().type).toBe("diff");
  });

  it("puts the switch before the owner badge and the file's menu after Viewed, last", () => {
    const owned = { ...reviewFile(MARKDOWN, "modified", { additions: 1 }), ownedByViewer: true };
    render(<DiffReview {...markdownProps({ files: [owned] })} />);
    const controls = header();
    const order = [
      controls.getByRole("button", { name: `Rich diff of ${MARKDOWN}` }),
      controls.getByRole("button", { name: "Owned by you" }),
      controls.getByRole("button", { name: `Viewed ${MARKDOWN}` }),
      controls.getByRole("button", { name: `More actions for ${MARKDOWN}` }),
    ];
    order.slice(1).forEach((later, index) => {
      expect(order[index]!.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    });
    expect(order[0]!.parentElement!.lastElementChild).toBe(order.at(-1));
  });

  it("offers the same switch in the file's menu and in ⌘K", () => {
    render(<DiffReview {...markdownProps()} />);
    const menu = header().getByRole("button", { name: `More actions for ${MARKDOWN}` });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    expect(screen.getByRole("menuitem", { name: "Show rich diff" })).toBeInTheDocument();
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    const command = commands.find((each) => each.label === "Show rich diff for guide.md");
    expect(command).toBeDefined();
    expect(command!.leadsSources).toBe(false);
    act(() => command!.run());
    expect(item().type).toBe("file");
    const updated = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    expect(updated.map((each) => each.label)).toContain("Show source diff for guide.md");
  });

  it("says when the file's comments are on its source diff", async () => {
    window.localStorage.setItem(
      "omnigent.diffReview.richMarkdownFiles",
      JSON.stringify([`test-review\u0000${MARKDOWN}`]),
    );
    const highlights = [{ path: MARKDOWN, start: 0, end: 4, active: false }];
    render(<DiffReview {...markdownProps({ highlights })} />);
    const annotation = (item() as { annotations: { lineNumber: number; metadata: unknown }[] })
      .annotations[0]!;
    const row = () => <>{codeView.props!.renderAnnotation!(annotation, item())}</>;
    const mounted = render(row());
    await settle();
    mounted.rerender(row());
    expect(
      await screen.findByText("Comments on this file appear in the source diff."),
    ).toBeVisible();
    act(() => fireEvent.click(screen.getByRole("button", { name: "Show source diff" })));
    expect(item().type).toBe("diff");
  });

  it("offers no switch for other files", () => {
    render(<DiffReview {...props(ready)} />);
    expect(header().queryByRole("button", { name: /^Rich diff of/ })).toBeNull();
  });

  it("remembers the choice for its review only", () => {
    const first = render(<DiffReview {...markdownProps()} />);
    clickRichToggle();
    first.unmount();
    render(<DiffReview {...markdownProps()} />);
    expect(item().type).toBe("file");
    cleanup();
    render(<DiffReview {...markdownProps({ reviewScope: "another-review" })} />);
    expect(item().type).toBe("diff");
  });

  it("finds a match in the source diff of a file too large for its rich diff", async () => {
    window.localStorage.setItem(
      "omnigent.diffReview.richMarkdownFiles",
      JSON.stringify([`test-review\u0000${MARKDOWN}`]),
    );
    // Without a worker the page diffs at most 100,000 characters, so this preview is unavailable.
    const filler = "Filler paragraph text.\n\n".repeat(5_000);
    const large: DiffReviewContent = {
      status: "ready",
      fileDiff: computeFileDiff({
        path: MARKDOWN,
        before: `${filler}Old ending.\n`,
        after: `${filler}The NEEDLE ending.\n`,
        ignoreWhitespace: false,
        revision: "1",
      }),
    };
    const largeProps = (overrides: Partial<DiffReviewProps> = {}) =>
      markdownProps({ contents: new Map([[MARKDOWN, large]]), ...overrides });
    const view = render(<DiffReview {...largeProps()} />);
    expect(item().type).toBe("file");
    view.rerender(<DiffReview {...largeProps({ findOpen: true })} />);
    await settle();
    fireEvent.change(screen.getByTestId("diff-review-find-input"), {
      target: { value: "NEEDLE" },
    });
    await settle();
    expect(item().type).toBe("diff");
    expect(lineScrolls().at(-1)).toEqual(expect.objectContaining({ lineNumber: 10_001 }));
  });

  it("shows the source diff for a new line request, and lets the reader switch back", async () => {
    window.localStorage.setItem(
      "omnigent.diffReview.richMarkdownFiles",
      JSON.stringify([`test-review\u0000${MARKDOWN}`]),
    );
    const view = render(<DiffReview {...markdownProps()} />);
    expect(item().type).toBe("file");
    const target: ReviewRevealTarget = { path: MARKDOWN, line: 3, align: "center" };
    view.rerender(<DiffReview {...markdownProps({ revealTarget: target })} />);
    await settle();
    expect(item().type).toBe("diff");
    expect(lineScrolls()).toEqual([expect.objectContaining({ lineNumber: 3 })]);
    clickRichToggle();
    await settle();
    expect(item().type).toBe("file");
    // The same request doesn't take the source diff back.
    view.rerender(<DiffReview {...markdownProps({ revealTarget: target })} />);
    await settle();
    expect(item().type).toBe("file");
  });
});

describe("the current file's actions in ⌘K", () => {
  const FIRST = "src/a.ts";
  const SECOND = "src/b.ts";
  const ran: string[] = [];
  const fileActions = (file: { path: string; change: string }): DiffReviewFileAction[] => [
    { id: "copy-path", label: "Copy path", icon: CopyIcon, run: () => ran.push(file.path) },
    {
      id: "reveal",
      label: "Show in Finder",
      icon: FolderOpenIcon,
      disabledReason: file.change === "deleted" ? "It was deleted." : undefined,
      run: () => ran.push(`reveal ${file.path}`),
    },
  ];
  const twoFiles = (
    overrides: Partial<DiffReviewProps> = {},
    secondChange: "modified" | "deleted" = "modified",
  ) =>
    props(ready, {
      files: [
        reviewFile(FIRST, "modified", { additions: 1, deletions: 1 }),
        reviewFile(SECOND, secondChange, { additions: 0, deletions: 1 }),
      ],
      contents: new Map([
        [FIRST, contentFor(FIRST)],
        [SECOND, contentFor(SECOND)],
      ]),
      revealPath: FIRST,
      fileActions,
      ...overrides,
    });
  const fileCommands = () =>
    Object.entries(usePaletteCommandsStore.getState().groups)
      .filter(([source]) => source.startsWith("diff-review-file"))
      .flatMap(([, group]) => group.commands);

  afterEach(() => {
    ran.length = 0;
    usePaletteCommandsStore.setState({ groups: {} });
    vi.restoreAllMocks();
    delete (HTMLElement.prototype as { scrollBy?: unknown }).scrollBy;
  });

  it("follows the file the reader scrolls to, with that file's availability", async () => {
    // The review keeps the reader's place with native scrolls jsdom lacks.
    HTMLElement.prototype.scrollBy = () => undefined;
    const view = render(<DiffReview {...twoFiles()} />);
    act(() =>
      fileCommands()
        .find((command) => command.id === "file.copy-path")!
        .run(),
    );
    expect(ran).toEqual([FIRST]);
    expect(
      isCommandAvailable(fileCommands().find((c) => c.id === "file.reveal")!.availability),
    ).toBe(true);

    // The second file's header reaches the viewport's top.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      const item = this.dataset.reviewItem ?? "";
      const [top, bottom] = item.endsWith(FIRST)
        ? [-400, -5]
        : item.endsWith(SECOND)
          ? [-5, 600]
          : [0, 600];
      return {
        top,
        bottom,
        left: 0,
        right: 0,
        width: 0,
        height: bottom - top,
        x: 0,
        y: top,
      } as DOMRect;
    });
    fireEvent.scroll(screen.getByTestId("code-view"));
    await settle();

    act(() =>
      fileCommands()
        .find((command) => command.id === "file.copy-path")!
        .run(),
    );
    expect(ran).toEqual([FIRST, SECOND]);
    // Once deleted, it can't be shown in the file manager; ⌘K says why.
    view.rerender(<DiffReview {...twoFiles({}, "deleted")} />);
    expect(fileCommands().find((command) => command.id === "file.reveal")!.availability).toEqual({
      available: false,
      reason: "It was deleted.",
    });
  });

  const folderCommands = () =>
    Object.entries(usePaletteCommandsStore.getState().groups)
      .filter(([source]) => source.startsWith("diff-review-folder"))
      .flatMap(([, group]) => group.commands);

  it("gives ⌘K a focused folder's menu actions instead of the file's, until focus leaves", async () => {
    const copied: string[] = [];
    render(
      <DiffReview
        {...twoFiles({
          onOpenFileInNewTab: () => undefined,
          folderActions: (path) => [
            { id: "copy-path", label: "Copy path", icon: CopyIcon, run: () => copied.push(path) },
          ],
        })}
      />,
    );
    const folder = await findTreeRow("src/");
    act(() => folder.focus());
    await waitFor(() =>
      expect(folderCommands().map((command) => command.label)).toEqual([
        "Collapse folder",
        "Copy path",
      ]),
    );
    // The file's own actions step aside, so Copy path can only mean the folder.
    expect(fileCommands().filter((command) => command.id.startsWith("file."))).toEqual([]);

    act(() =>
      folderCommands()
        .find((command) => command.id === "folder.collapse-folder")!
        .run(),
    );
    await waitFor(() => expect(treeRowPaths()).toEqual(["src/"]));
    // The command follows the folder's state, as its menu does.
    await waitFor(() =>
      expect(folderCommands().map((command) => command.label)).toEqual([
        "Expand folder",
        "Copy path",
      ]),
    );
    act(() =>
      folderCommands()
        .find((command) => command.id === "folder.copy-path")!
        .run(),
    );
    expect(copied).toEqual(["src"]);

    // Focus moving out of the tree hands ⌘K back to the review's current file.
    const elsewhere = document.createElement("button");
    document.body.append(elsewhere);
    act(() => elsewhere.focus());
    await waitFor(() => expect(folderCommands()).toEqual([]));
    expect(fileCommands().map((command) => command.id)).toContain("file.copy-path");
    elsewhere.remove();
  });

  const showTestFiles = (value: "Skip test files" | "Only test files") => {
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    act(() => void chooseSettingValue(settingIn(commands, "Test files"), value));
  };
  const filteredReview = (paths: string[]) =>
    props(ready, {
      files: paths.map((path) => reviewFile(path, "modified", { additions: 1, deletions: 1 })),
      contents: new Map(paths.map((path) => [path, contentFor(path)])),
      revealPath: undefined,
      fileActions,
    });

  it("moves to the listed file in its place when filters hide the current one", () => {
    const TEST = "src/b.test.ts";
    render(<DiffReview {...filteredReview([FIRST, TEST])} />);
    act(() =>
      fileCommands()
        .find((command) => command.id === "file.copy-path")!
        .run(),
    );
    expect(ran).toEqual([FIRST]);
    // All → only tests from ⌘K, with no scroll in between.
    showTestFiles("Only test files");
    act(() =>
      fileCommands()
        .find((command) => command.id === "file.copy-path")!
        .run(),
    );
    expect(ran).toEqual([FIRST, TEST]);
  });

  it("withdraws the file's actions when the filters list no file", () => {
    render(<DiffReview {...filteredReview([FIRST])} />);
    expect(fileCommands()).not.toEqual([]);
    showTestFiles("Only test files");
    expect(screen.getByText("No matching files")).toBeInTheDocument();
    expect(fileCommands()).toEqual([]);
  });

  it("offers the current file in a new tab where the surface has tabs", () => {
    const opened: [string, { background: boolean }][] = [];
    const view = render(
      <DiffReview
        {...twoFiles({ onOpenFileInNewTab: (path, options) => opened.push([path, options]) })}
      />,
    );
    const newTab = fileCommands().find((command) => command.label === `Open ${FIRST} in a new tab`);
    expect(newTab).toBeDefined();
    // Named after the file, it stays below sessions that match the file's name.
    expect(newTab!.leadsSources).toBe(false);
    act(() => newTab!.run());
    expect(opened).toEqual([[FIRST, { background: true }]]);
    // The file's own actions stay as the source gave them, under "File".
    expect(fileCommands().filter((command) => command.id.startsWith("file."))).toHaveLength(2);

    view.rerender(<DiffReview {...twoFiles()} />);
    expect(fileCommands().some((command) => command.label.endsWith("in a new tab"))).toBe(false);
  });

  it("lists Open in new tab first in each file's menu", () => {
    const opened: string[] = [];
    render(<DiffReview {...twoFiles({ onOpenFileInNewTab: (path) => opened.push(path) })} />);
    // The code view is a stand-in here, so the test draws the file's header itself.
    const item = codeView.props!.items.find((each) => each.id.endsWith(SECOND))!;
    const header = within(
      render(
        <TooltipProvider>
          {(
            codeView.props as { renderHeaderMetadata?: (item: unknown) => ReactNode }
          ).renderHeaderMetadata?.(item)}
        </TooltipProvider>,
      ).container,
    );
    const menu = header.getByRole("button", { name: `More actions for ${SECOND}` });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    const items = screen.getAllByRole("menuitem").map((menuItem) => menuItem.textContent);
    expect(items[0]).toBe("Open in new tab");
    expect(items).toContain("Copy path");
    fireEvent.click(screen.getByRole("menuitem", { name: "Open in new tab" }));
    expect(opened).toEqual([SECOND]);
  });

  it("leaves ⌘K's file to whoever owns it while the review doesn't", () => {
    const view = render(<DiffReview {...twoFiles({ fileActionsInPalette: false })} />);
    expect(fileCommands()).toEqual([]);
    view.rerender(<DiffReview {...twoFiles({ commandsActive: false })} />);
    expect(fileCommands()).toEqual([]);
  });
});

describe("listedFileAt", () => {
  const [first, second, third] = ["a.ts", "b.ts", "c.ts"].map((path) =>
    reviewFile(path, "modified", { additions: 1, deletions: 1 }),
  );
  const all = [first!, second!, third!];

  it("keeps a listed file, or takes the next listed one in its place", () => {
    expect(listedFileAt("b.ts", all, all)?.path).toBe("b.ts");
    expect(listedFileAt("b.ts", all, [first!, third!])?.path).toBe("c.ts");
  });

  it("falls back to the last listed file, or the first for an unknown path", () => {
    expect(listedFileAt("c.ts", all, [first!, second!])?.path).toBe("b.ts");
    expect(listedFileAt(null, all, [second!, third!])?.path).toBe("b.ts");
    expect(listedFileAt("gone.ts", all, [third!])?.path).toBe("c.ts");
  });

  it("has no file when nothing is listed", () => {
    expect(listedFileAt("a.ts", all, [])).toBeUndefined();
  });
});

describe("file headers", () => {
  const FIRST = "src/app/main.ts";
  const SECOND = "src/b.ts";
  const copied: string[] = [];
  const fileActions = (file: { path: string }): DiffReviewFileAction[] => [
    { id: "copy-path", label: "Copy path", icon: CopyIcon, run: () => copied.push(file.path) },
  ];
  const review = (overrides: Partial<DiffReviewProps> = {}, secondAfter = "new\n") =>
    props(ready, {
      files: [FIRST, SECOND].map((path) =>
        reviewFile(path, "modified", { additions: 1, deletions: 1 }),
      ),
      contents: new Map([
        [FIRST, contentFor(FIRST)],
        [SECOND, contentFor(SECOND, secondAfter)],
      ]),
      revealPath: FIRST,
      reviewScope: "test-review",
      fileActions,
      ...overrides,
    });
  const itemFor = (path: string) => codeView.props!.items.find((item) => item.id.endsWith(path))!;
  const header = (path: string) =>
    within(
      render(
        <TooltipProvider>
          {codeView.props!.renderHeaderPrefix?.(itemFor(path))}
          {(
            codeView.props as { renderHeaderFilenameSuffix?: (item: unknown) => ReactNode }
          ).renderHeaderFilenameSuffix?.(itemFor(path))}
          {codeView.props!.renderHeaderMetadata!(itemFor(path))}
        </TooltipProvider>,
      ).container,
    );
  const viewedToggle = (path: string) =>
    header(path).getByRole("button", { name: `Viewed ${path}` });

  const paletteCommand = (id: string) =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((each) => each.id === id);

  const clickViewed = (path: string) => {
    const toggle = viewedToggle(path);
    act(() => fireEvent.click(toggle));
  };

  afterEach(() => {
    copied.length = 0;
    window.localStorage.clear();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  it("shows the folder and the bold name, and copies the path with the shared action", () => {
    render(<DiffReview {...review()} />);
    const title = header(FIRST);
    expect(title.getByText("src/app/")).toHaveClass("text-muted-foreground");
    expect(title.getByText("main.ts")).toHaveClass("font-semibold");
    fireEvent.click(title.getByRole("button", { name: `Copy path ${FIRST}` }));
    expect(copied).toEqual([FIRST]);
  });

  it("repeats Copy path in the ⋯ menu only while the header hides the title's button", () => {
    const opened: string[] = [];
    const withHostLink = (file: { path: string }): DiffReviewFileAction[] => [
      {
        id: "open-on-code-host",
        label: "Open in GitHub",
        icon: CopyIcon,
        run: () => opened.push(file.path),
      },
      {
        id: "copy-path",
        label: "Copy path",
        paletteLabel: "Copy Path of Active File",
        icon: CopyIcon,
        run: () => copied.push(file.path),
      },
      {
        id: "reveal",
        label: "Show in Finder",
        icon: CopyIcon,
        disabledReason: "Not on this Mac.",
        run: () => undefined,
      },
    ];
    render(<DiffReview {...review({ fileActions: withHostLink })} />);
    // The title and the menu render into the same header, as the code view slots them.
    const title = header(FIRST);
    const copyButton = title.getByRole("button", { name: `Copy path ${FIRST}` });
    const menuRows = () => {
      fireEvent.pointerDown(title.getByRole("button", { name: `More actions for ${FIRST}` }), {
        button: 0,
        ctrlKey: false,
      });
      const rows = screen.getAllByRole("menuitem").map((row) => row.textContent);
      fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
      return rows;
    };
    expect(menuRows()).toEqual(["Open in GitHub", "Show in Finder"]);

    // A narrow header hides the title's button, so the menu offers copying instead,
    // measured again on every opening, from the keyboard too.
    let titleButtonShown = false;
    copyButton.checkVisibility = () => titleButtonShown;
    expect(menuRows()).toEqual(["Open in GitHub", "Copy path", "Show in Finder"]);
    titleButtonShown = true;
    expect(menuRows()).toEqual(["Open in GitHub", "Show in Finder"]);
    titleButtonShown = false;
    const trigger = title.getByRole("button", { name: `More actions for ${FIRST}` });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    act(() => fireEvent.click(screen.getByRole("menuitem", { name: "Copy path" })));
    expect(copied).toEqual([FIRST]);

    fireEvent.pointerDown(title.getByRole("button", { name: `More actions for ${FIRST}` }), {
      button: 0,
      ctrlKey: false,
    });
    act(() => fireEvent.click(screen.getByRole("menuitem", { name: "Open in GitHub" })));
    expect(opened).toEqual([FIRST]);

    // ⌘K lists every action, whatever the header shows, by its palette name where it has one.
    const commands = Object.entries(usePaletteCommandsStore.getState().groups)
      .filter(([source]) => source.startsWith("diff-review-file"))
      .flatMap(([, group]) => group.commands);
    expect(commands.map((command) => [command.id, command.label])).toEqual([
      ["file.open-on-code-host", "Open in GitHub"],
      ["file.copy-path", "Copy Path of Active File"],
      ["file.reveal", "Show in Finder"],
    ]);
    expect(commands[2]!.availability).toEqual({ available: false, reason: "Not on this Mac." });
  });

  it("keeps Copy path in a menu that has nothing else", () => {
    render(<DiffReview {...review()} />);
    const title = header(FIRST);
    fireEvent.pointerDown(title.getByRole("button", { name: `More actions for ${FIRST}` }), {
      button: 0,
      ctrlKey: false,
    });
    expect(screen.getAllByRole("menuitem").map((row) => row.textContent)).toEqual(["Copy path"]);
  });

  it("offers no copy button without a copy-path action", () => {
    render(<DiffReview {...review({ fileActions: undefined })} />);
    expect(header(FIRST).queryByRole("button", { name: /^Copy path/ })).toBeNull();
  });

  it("folds a viewed file and remembers it for the review, until it's cleared", () => {
    const first = render(<DiffReview {...review()} />);
    expect(itemFor(SECOND).collapsed).toBe(false);
    clickViewed(SECOND);
    expect(itemFor(SECOND).collapsed).toBe(true);
    expect(viewedToggle(SECOND)).toHaveAttribute("aria-pressed", "true");

    // Opening the review again keeps the mark.
    first.unmount();
    const second = render(<DiffReview {...review()} />);
    expect(itemFor(SECOND).collapsed).toBe(true);
    expect(itemFor(FIRST).collapsed).toBe(false);
    clickViewed(SECOND);
    expect(itemFor(SECOND).collapsed).toBe(false);
    expect(viewedToggle(SECOND)).toHaveAttribute("aria-pressed", "false");
    second.unmount();
    render(<DiffReview {...review({ reviewScope: "another-review" })} />);
    expect(itemFor(SECOND).collapsed).toBe(false);
  });

  it("leaves a viewed file out of the toolbar's line counts, and counts it again once cleared", () => {
    render(<DiffReview {...review()} />);
    const lineCounts = () => screen.getByTestId("diff-review-line-counts");
    const allCounts = lineCounts().textContent;
    clickViewed(SECOND);
    expect(lineCounts()).toHaveAccessibleName(/ in files not viewed\. /);
    expect(lineCounts().textContent).not.toBe(allCounts);
    clickViewed(SECOND);
    expect(lineCounts().textContent).toBe(allCounts);
    expect(lineCounts()).not.toHaveAccessibleName(/not viewed/);
  });

  it("opens a viewed file again once its changes are edited", () => {
    const view = render(<DiffReview {...review()} />);
    clickViewed(SECOND);
    expect(itemFor(SECOND).collapsed).toBe(true);
    view.rerender(<DiffReview {...review({}, "edited again\n")} />);
    expect(itemFor(SECOND).collapsed).toBe(false);
    expect(viewedToggle(SECOND)).toHaveAttribute("aria-pressed", "false");
  });

  it("waits for a loading file before marking it, so a mark always names a revision", () => {
    const firstLoading = (): Partial<DiffReviewProps> => ({
      contents: new Map<string, DiffReviewContent>([
        [FIRST, { status: "loading" }],
        [SECOND, contentFor(SECOND)],
      ]),
    });
    const view = render(<DiffReview {...review(firstLoading())} />);
    expect(viewedToggle(FIRST)).toHaveAttribute("aria-disabled", "true");
    clickViewed(FIRST);
    expect(viewedToggle(FIRST)).toHaveAttribute("aria-pressed", "false");
    expect(paletteCommand("toggle-viewed")).toBeUndefined();

    view.rerender(<DiffReview {...review()} />);
    expect(viewedToggle(FIRST)).not.toHaveAttribute("aria-disabled");
    clickViewed(FIRST);
    expect(itemFor(FIRST).collapsed).toBe(true);
    // The mark names these changes, so an edit opens the file again.
    view.rerender(
      <DiffReview
        {...review({
          contents: new Map([
            [FIRST, contentFor(FIRST, "edited\n")],
            [SECOND, contentFor(SECOND)],
          ]),
        })}
      />,
    );
    expect(itemFor(FIRST).collapsed).toBe(false);
  });

  it("marks an undiffable file only when its source names the content", () => {
    const unavailable = (revision?: string): Partial<DiffReviewProps> => ({
      contents: new Map<string, DiffReviewContent>([
        [FIRST, { status: "unavailable", message: "Binary file", revision }],
        [SECOND, contentFor(SECOND)],
      ]),
    });
    const view = render(<DiffReview {...review(unavailable())} />);
    expect(viewedToggle(FIRST)).toHaveAttribute("aria-disabled", "true");
    view.rerender(<DiffReview {...review(unavailable("blob-1"))} />);
    clickViewed(FIRST);
    expect(viewedToggle(FIRST)).toHaveAttribute("aria-pressed", "true");
    // Replaced content with the same line counts is a new revision.
    view.rerender(<DiffReview {...review(unavailable("blob-2"))} />);
    expect(viewedToggle(FIRST)).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps the viewed fold of the file a reload opens on, and reveals it when opened later", () => {
    const first = render(<DiffReview {...review()} />);
    clickViewed(FIRST);
    first.unmount();

    const reloaded = render(<DiffReview {...review()} />);
    expect(itemFor(FIRST).collapsed).toBe(true);
    reloaded.rerender(<DiffReview {...review({ revealPath: SECOND })} />);
    reloaded.rerender(<DiffReview {...review({ revealPath: FIRST })} />);
    expect(itemFor(FIRST).collapsed).toBe(false);
  });

  it("offers no viewed marks in a review that can't keep them, and ignores marks already kept", () => {
    const marked = render(<DiffReview {...review({ commandHeading: "Changes" })} />);
    clickViewed(SECOND);
    marked.unmount();
    const kept = window.localStorage.getItem("omnigent.diffReview.viewedFiles");
    render(<DiffReview {...review({ commandHeading: "Changes", viewedMarks: false })} />);
    expect(header(SECOND).queryByRole("button", { name: `Viewed ${SECOND}` })).toBeNull();
    // A mark made in the review's scope elsewhere doesn't fold the file here.
    expect(itemFor(SECOND).collapsed).toBe(false);
    for (const id of ["toggle-viewed", "mark-all-viewed", "mark-all-not-viewed"]) {
      expect(paletteCommand(id)).toBeUndefined();
    }
    expect(window.localStorage.getItem("omnigent.diffReview.viewedFiles")).toBe(kept);
  });

  it("marks the current file viewed from ⌘K", () => {
    render(<DiffReview {...review({ commandHeading: "Changes" })} />);
    const command = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((each) => each.id === "toggle-viewed")!;
    expect(command().label).toBe("Mark main.ts as viewed");
    expect(command().leadsSources).toBe(false);
    act(() => command().run());
    expect(itemFor(FIRST).collapsed).toBe(true);
    expect(command().label).toBe("Mark main.ts as not viewed");
  });

  it("marks every file viewed from ⌘K, and clears every mark", () => {
    render(<DiffReview {...review({ commandHeading: "Changes" })} />);
    expect(paletteCommand("mark-all-not-viewed")).toBeUndefined();
    const markAll = paletteCommand("mark-all-viewed")!;
    expect(markAll.label).toBe("Mark all files as viewed");
    act(() => markAll.run());
    expect(itemFor(FIRST).collapsed).toBe(true);
    expect(itemFor(SECOND).collapsed).toBe(true);
    expect(paletteCommand("mark-all-viewed")).toBeUndefined();

    const clearAll = paletteCommand("mark-all-not-viewed")!;
    expect(clearAll.label).toBe("Mark all files as not viewed");
    act(() => clearAll.run());
    expect(itemFor(FIRST).collapsed).toBe(false);
    expect(viewedToggle(SECOND)).toHaveAttribute("aria-pressed", "false");
  });

  it("waits for loading files before marking every file viewed", () => {
    const view = render(
      <DiffReview
        {...review({
          commandHeading: "Changes",
          contents: new Map<string, DiffReviewContent>([
            [FIRST, { status: "loading" }],
            [SECOND, contentFor(SECOND)],
          ]),
        })}
      />,
    );
    expect(paletteCommand("mark-all-viewed")!.availability).toEqual({
      available: false,
      reason: MARK_ALL_VIEWED_LOADING_REASON,
    });
    view.rerender(<DiffReview {...review({ commandHeading: "Changes" })} />);
    expect(paletteCommand("mark-all-viewed")!.availability).toEqual({ available: true });
  });
});

describe("DiffReview file problems", () => {
  const header = () =>
    within(
      render(
        <TooltipProvider>
          {codeView.props!.renderHeaderMetadata!(codeView.props!.items[0]!)}
        </TooltipProvider>,
      ).container,
    );

  // A narrow header keeps only the retry icon, so it carries the whole explanation.
  it.each([
    [
      "a failed load",
      { status: "failed", message: "git status timed out after 5.0s" } as DiffReviewContent,
      "git status timed out after 5.0s",
    ],
    [
      "a failed refresh",
      { ...ready, refreshError: "git status timed out after 5.0s" } as DiffReviewContent,
      "Showing an earlier version. git status timed out after 5.0s",
    ],
  ])("explains %s from its retry button on focus", async (_name, content, explanation) => {
    const onRetry = vi.fn();
    render(<DiffReview {...props(content, { onRetry })} />);
    const retry = header().getByRole("button", { name: "Try again" });
    act(() => retry.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent(explanation);
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledWith(PATH);
  });

  // A narrow header shows the note as an icon, so the note itself must carry its text.
  it("keeps a file's note reachable by name and shows it in full when tapped", async () => {
    const note = "No text diff available for this file.";
    render(
      <DiffReview {...props({ status: "unavailable", message: note } as DiffReviewContent)} />,
    );
    const status = header().getByRole("button", { name: note });
    expect(status.className).not.toMatch(/(^|\s)@max-\[22rem\]:hidden(\s|$)/);
    fireEvent.click(status);
    expect(await screen.findByRole("dialog")).toHaveTextContent(note);
  });
});

describe("an opt-in presentation", () => {
  const whitespaceOnly: DiffReviewContent = {
    status: "ready",
    fileDiff: computeFileDiff({
      path: PATH,
      before: "let a = 1;\n",
      after: "  let a = 1;\n",
      ignoreWhitespace: true,
      revision: "1",
    }),
  };

  it("renders the toolbar from the review's own state and handlers", () => {
    const toolbar = vi.fn((toolbarProps: DiffReviewToolbarProps) => (
      <div data-testid="reference-toolbar">{toolbarProps.fileCount} file</div>
    ));
    render(<DiffReview {...props(ready, { presentation: { toolbar } })} />);
    expect(screen.getByTestId("reference-toolbar")).toHaveTextContent("1 file");
    const given = toolbar.mock.calls.at(-1)![0];
    expect(given.breakdown.totals).toEqual({ files: 1, additions: 1, deletions: 1 });
    expect(given.commands.length).toBeGreaterThan(0);
    expect(given.dispatchFilter).toBeTypeOf("function");
    expect(given.setFiltersOpen).toBeTypeOf("function");
  });

  it("marks added and deleted files by icon only with fileBadges icons", () => {
    const added = props(ready, {
      files: [reviewFile(PATH, "added", { additions: 1, deletions: 0 })],
    });
    const header = () =>
      render(
        <TooltipProvider>
          {codeView.props?.renderHeaderMetadata?.(codeView.items[0])}
        </TooltipProvider>,
      ).container;
    render(<DiffReview {...added} />);
    expect(header()).toHaveTextContent("Added");
    cleanup();
    render(<DiffReview {...added} presentation={{ fileBadges: "icons" }} />);
    expect(header()).not.toHaveTextContent("Added");
  });

  it("uses its code size and line height, and the Appearance font's without", () => {
    render(
      <DiffReview
        {...props(ready, { presentation: { codeMetrics: { fontSizePx: 10.5, lineHeightPx: 18 } } })}
      />,
    );
    expect(codeView.props?.options.itemMetrics).toMatchObject({ lineHeight: 18 });
    expect(screen.getByTestId("diff-review").style.getPropertyValue("--diffs-font-size")).toBe(
      "10.5px",
    );
    cleanup();
    render(<DiffReview {...props(ready)} />);
    expect(screen.getByTestId("diff-review").style.getPropertyValue("--diffs-font-size")).not.toBe(
      "10.5px",
    );
  });

  it("shows its own empty message", () => {
    render(
      <DiffReview
        {...props(ready, {
          files: [],
          presentation: {
            messages: {
              empty: { title: "Working Copy Is Clean", message: "There are no changes." },
            },
          },
        })}
      />,
    );
    expect(screen.getByText("Working Copy Is Clean")).toBeInTheDocument();
    expect(screen.getByText("There are no changes.")).toBeInTheDocument();
  });

  it("says only whitespace changed once ignoring it leaves nothing, and only when asked to", () => {
    const onlyWhitespace = {
      title: "Only Whitespace Changed",
      message: "No changes remain when whitespace is ignored.",
    };
    render(
      <DiffReview
        {...props(whitespaceOnly, {
          ignoreWhitespace: true,
          presentation: { messages: { onlyWhitespace } },
        })}
      />,
    );
    expect(screen.getByText("Only Whitespace Changed")).toBeInTheDocument();
    expect(screen.queryByTestId("code-view")).toBeNull();
    cleanup();
    // Still loading: not known yet, so the review shows the file.
    render(
      <DiffReview
        {...props(loading, {
          ignoreWhitespace: true,
          presentation: { messages: { onlyWhitespace } },
        })}
      />,
    );
    expect(screen.queryByText("Only Whitespace Changed")).toBeNull();
    cleanup();
    render(<DiffReview {...props(whitespaceOnly, { ignoreWhitespace: true })} />);
    expect(screen.queryByText("Only Whitespace Changed")).toBeNull();
    expect(screen.getByTestId("code-view")).toBeInTheDocument();
  });

  it("lists a tree row's file menu without a new-tab callback, only when asked to", async () => {
    const opened: string[] = [];
    const fileActions = (file: { path: string }) => [
      { id: "view-file", label: "View File", icon: CopyIcon, run: () => opened.push(file.path) },
    ];
    render(
      <DiffReview {...props(ready, { fileActions })} presentation={{ treeRowActions: true }} />,
    );
    fireEvent.contextMenu(await findTreeRow(PATH));
    fireEvent.click(await screen.findByRole("menuitem", { name: "View File" }));
    expect(opened).toEqual([PATH]);
    cleanup();
    render(<DiffReview {...props(ready, { fileActions })} />);
    fireEvent.contextMenu(await findTreeRow(PATH));
    expect(screen.queryByTestId("diff-review-tree-row-menu")).toBeNull();
  });

  it("gives the tree a keyboard-reachable edge with its own limits", () => {
    render(
      <DiffReview {...props(ready, { presentation: { treeColumn: CHANGES_REVIEW_TREE_WIDTH } })} />,
    );
    const edge = screen.getByRole("separator", { name: "Resize changed files" });
    expect(edge).toHaveAttribute("aria-valuenow", "228");
    fireEvent.keyDown(edge, { key: "End" });
    expect(edge).toHaveAttribute("aria-valuenow", "360");
  });

  it("gives a review without limits of its own the pull request tree's, with a keyboard edge", () => {
    render(<DiffReview {...props(ready)} />);
    const edge = screen.getByRole("separator", { name: "Resize changed files" });
    expect(edge).toHaveAttribute("tabindex", "0");
    expect(edge).toHaveAttribute("aria-valuemin", "160");
    expect(edge).toHaveAttribute("aria-valuenow", "240");
    fireEvent.keyDown(edge, { key: "ArrowRight", shiftKey: true });
    expect(edge).toHaveAttribute("aria-valuenow", "272");
  });
});

describe("the opt-in filter rows in ⌘K", () => {
  afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

  const files = [
    reviewFile("src/a.ts", "modified", { additions: 1, deletions: 1 }),
    reviewFile("tools/b.py", "modified", { additions: 1, deletions: 1 }),
    {
      ...reviewFile("pnpm-lock.yaml", "modified", { additions: 9, deletions: 9 }),
      isGenerated: true,
    },
  ];
  const withFiles = (overrides: Partial<DiffReviewProps> = {}) =>
    props(ready, { files, contents: new Map(), commandHeading: "Changes", ...overrides });
  const labels = () =>
    toolbarCommandGroups().flatMap((group) => group.commands.map((command) => command.label));

  it("lists Generated files and Languages for a review that asks, and runs the shared filters", () => {
    render(<DiffReview {...withFiles()} presentation={{ filterCommands: true }} />);
    const commands = toolbarCommandGroups()[0]!.commands;
    expectSetting(commands, "Generated files", { current: "Show", choices: ["Show", "Hide"] });
    const languages = expectSetting(commands, "Languages", { current: "All languages" });
    expect(languages.choices.map((choice) => choice.label)).toContain("TypeScript (1)");
    act(
      () =>
        void chooseSettingValue(
          settingIn(toolbarCommandGroups()[0]!.commands, "Languages"),
          "TypeScript (1)",
        ),
    );
    expectSetting(toolbarCommandGroups()[0]!.commands, "Languages", { current: "TypeScript (1)" });
    act(
      () =>
        void chooseSettingValue(
          settingIn(toolbarCommandGroups()[0]!.commands, "Generated files"),
          "Hide",
        ),
    );
    expectSetting(toolbarCommandGroups()[0]!.commands, "Generated files", { current: "Hide" });
  });

  it("leaves them out without the presentation, and while the review is hidden", () => {
    const { rerender } = render(<DiffReview {...withFiles()} />);
    expect(labels()).not.toContain("Generated files");
    expect(labels()).not.toContain("Languages");
    rerender(
      <DiffReview
        {...withFiles({ commandsActive: false })}
        presentation={{ filterCommands: true }}
      />,
    );
    expect(labels()).toEqual([]);
  });
});

describe("DiffReview owned files filter", () => {
  const OWNED = "src/owned.ts";
  const OTHER = "src/other.test.ts";
  const ownedReview = (overrides: Partial<DiffReviewProps> = {}, owned = true) =>
    props(ready, {
      files: [
        { ...reviewFile(OWNED, "modified", { additions: 1 }), ownedByViewer: owned },
        reviewFile(OTHER, "modified", { additions: 1 }),
      ],
      contents: new Map([
        [OWNED, contentFor(OWNED)],
        [OTHER, contentFor(OTHER)],
      ]),
      revealPath: undefined,
      reviewScope: "pr-a",
      ...overrides,
    });
  const listed = () => codeView.props!.items.map((item) => pathFromReviewItemId(item.id));
  const onHeaderClick = vi.fn();
  /** The owned file's badge; a click that reaches the header would fold the file. */
  const badge = () => {
    const item = codeView.props!.items.find((each) => each.id.endsWith(OWNED))!;
    const header = render(
      <TooltipProvider>
        <div onClick={onHeaderClick}>{codeView.props!.renderHeaderMetadata!(item)}</div>
      </TooltipProvider>,
    );
    return within(header.container).getByRole("button", { name: "Owned by you" });
  };
  const chipRemove = () => screen.queryByRole("button", { name: "Remove Owned by you filter" });

  afterEach(() => {
    window.localStorage.clear();
    resetUnsavedOwnedFilesOnly();
    usePaletteCommandsStore.setState({ groups: {} });
    onHeaderClick.mockClear();
  });

  it("shows only your files from the badge, and all again from the badge or the chip", async () => {
    const user = userEvent.setup();
    render(<DiffReview {...ownedReview()} />);
    expect(badge()).toHaveAttribute("aria-pressed", "false");
    expect(chipRemove()).toBeNull();

    fireEvent.click(badge());
    expect(listed()).toEqual([OWNED]);
    expect(badge()).toHaveAttribute("aria-pressed", "true");
    expect(chipRemove()).toBeInTheDocument();
    fireEvent.click(badge());
    expect(listed()).toEqual([OTHER, OWNED]);
    expect(chipRemove()).toBeNull();

    // From the keyboard too, without folding the file.
    badge().focus();
    await user.keyboard("{Enter}");
    expect(listed()).toEqual([OWNED]);
    await user.click(chipRemove()!);
    expect(listed()).toEqual([OTHER, OWNED]);
    expect(screen.getByRole("button", { name: "Filter changed files" })).toHaveFocus();
    badge().focus();
    await user.keyboard(" ");
    expect(listed()).toEqual([OWNED]);
    expect(onHeaderClick).not.toHaveBeenCalled();
  });

  it("remembers the choice per review, applying it once ownership arrives", () => {
    writeOwnedFilesOnly("pr-a", true);
    // Before ownership is known, every file shows and nothing claims a filter.
    const view = render(<DiffReview {...ownedReview({}, false)} />);
    expect(listed()).toEqual([OTHER, OWNED]);
    expect(chipRemove()).toBeNull();
    view.rerender(<DiffReview {...ownedReview()} />);
    expect(listed()).toEqual([OWNED]);
    expect(badge()).toHaveAttribute("aria-pressed", "true");

    // Another review on the same mount starts from its own choice, and leaves A's alone.
    view.rerender(<DiffReview {...ownedReview({ reviewScope: "pr-b" })} />);
    expect(listed()).toEqual([OTHER, OWNED]);
    expect(chipRemove()).toBeNull();
    view.rerender(<DiffReview {...ownedReview()} />);
    expect(listed()).toEqual([OWNED]);
    expect(readOwnedFilesOnly("pr-b")).toBe(false);
  });

  it("keeps a linked file listed under the saved choice until the reader changes a filter", () => {
    writeOwnedFilesOnly("pr-a", true);
    render(<DiffReview {...ownedReview({ revealPath: OTHER })} />);
    // Following a link never lands on a hidden file.
    expect(listed()).toEqual([OTHER, OWNED]);
    fireEvent.click(badge());
    fireEvent.click(badge());
    expect(listed()).toEqual([OWNED]);
  });

  it("forgets the choice when the filters are cleared", async () => {
    const user = userEvent.setup();
    render(<DiffReview {...ownedReview()} />);
    fireEvent.click(badge());
    await user.click(screen.getByRole("button", { name: "Filter changed files" }));
    await user.click(screen.getByRole("menuitem", { name: "Clear filters" }));
    expect(listed()).toEqual([OTHER, OWNED]);
    expect(readOwnedFilesOnly("pr-a")).toBe(false);
  });

  it("says nothing matches when another filter hides every file you own", () => {
    render(<DiffReview {...ownedReview()} />);
    fireEvent.click(badge());
    const commands = Object.values(usePaletteCommandsStore.getState().groups).flatMap(
      (group) => group.commands,
    );
    act(() => void chooseSettingValue(settingIn(commands, "Test files"), "Only test files"));
    expect(screen.getByText("No matching files")).toBeInTheDocument();
    // The chip stays, so the way back is in view.
    expect(chipRemove()).toBeInTheDocument();
  });
});

describe("the toolbar's changes by language", () => {
  afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

  const TS_A = "src/a.ts";
  const TS_B = "src/b.ts";
  const PY_TEST = "tests/test_c.py";
  const files = [
    reviewFile(TS_A, "modified", { additions: 3, deletions: 1 }),
    reviewFile(TS_B, "added", { additions: 1, deletions: 0 }),
    reviewFile(PY_TEST, "modified", { additions: 5, deletions: 5 }),
  ];
  const review = (overrides: Partial<DiffReviewProps> = {}) =>
    props(ready, {
      files,
      contents: new Map(),
      revealPath: undefined,
      commandHeading: "Changes",
      ...overrides,
    });
  const listed = () => codeView.props!.items.map((item) => pathFromReviewItemId(item.id));
  const lineCounts = () => screen.getByTestId("diff-review-line-counts");
  const breakdownCommand = () =>
    toolbarCommandGroups()
      .flatMap((group) => group.commands)
      .find((command) => command.label === "Show changes by language");
  const popover = () => screen.getByRole("dialog", { name: "Changes by language" });
  const row = (language: string) =>
    within(popover()).getByRole("button", { name: new RegExp(`^${language}`) });

  it("opens from ⌘K too, without the filter presentation, with focus in the popover", () => {
    const view = render(<DiffReview {...review()} />);
    const command = breakdownCommand()!;
    expect(isCommandAvailable(command.availability)).toBe(true);
    act(() => command.run());
    expect(popover().contains(document.activeElement)).toBe(true);
    expect(lineCounts()).toHaveTextContent("+9−6");
    view.rerender(<DiffReview {...review({ commandsActive: false })} />);
    expect(breakdownCommand()).toBeUndefined();
  });

  it("filters the list to a chosen language, and its totals follow", async () => {
    const user = userEvent.setup();
    // A linked file outside the chosen language leaves with the filter change.
    render(<DiffReview {...review({ revealPath: PY_TEST })} />);
    await user.click(lineCounts());
    await user.click(row("TypeScript"));
    expect(listed()).toEqual([TS_A, TS_B]);
    expect(lineCounts()).toHaveTextContent("+4−1");
    await user.click(lineCounts());
    expect(row("TypeScript")).toHaveAttribute("aria-pressed", "true");
    await user.click(row("TypeScript"));
    expect(listed()).toEqual([TS_A, TS_B, PY_TEST]);
  });

  it("presses nothing when another filter happens to leave one language", async () => {
    const user = userEvent.setup();
    render(<DiffReview {...review()} />);
    await user.click(screen.getByRole("button", { name: "Skip test files" }));
    expect(listed()).toEqual([TS_A, TS_B]);
    await user.click(lineCounts());
    expect(row("TypeScript")).toHaveAttribute("aria-pressed", "false");
    expect(within(popover()).queryByRole("button", { name: "Show all languages" })).toBeNull();
  });

  it("closes for good when its review is hidden, without taking focus back", async () => {
    const user = userEvent.setup();
    const view = (active: boolean) => (
      <>
        <button type="button">Another view</button>
        <section hidden={!active}>
          <DiffReview {...review({ commandsActive: active })} />
        </section>
      </>
    );
    const { rerender } = render(view(true));
    await user.click(lineCounts());
    expect(popover().contains(document.activeElement)).toBe(true);
    // Hidden while the reader is in the popover, as when the workspace rail is hidden.
    rerender(view(false));
    expect(screen.queryByRole("dialog", { name: "Changes by language" })).toBeNull();
    expect(document.activeElement).not.toBe(lineCounts());
    const other = screen.getByRole("button", { name: "Another view" });
    other.focus();
    // Shown again, it stays closed and leaves focus where the reader put it.
    rerender(view(true));
    expect(screen.queryByRole("dialog", { name: "Changes by language" })).toBeNull();
    expect(document.activeElement).toBe(other);
  });

  it("stays closed when its files return after the list emptied", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<DiffReview {...review()} />);
    await user.click(lineCounts());
    expect(popover()).toBeVisible();
    rerender(<DiffReview {...review({ files: [] })} />);
    expect(screen.queryByRole("dialog", { name: "Changes by language" })).toBeNull();
    rerender(<DiffReview {...review()} />);
    expect(screen.queryByRole("dialog", { name: "Changes by language" })).toBeNull();
    expect(document.activeElement).not.toBe(lineCounts());
  });

  it("can't open while the filters leave no file listed", async () => {
    const user = userEvent.setup();
    render(<DiffReview {...review({ files: [files[2]!] })} />);
    await user.click(screen.getByRole("button", { name: "Skip test files" }));
    // The code view isn't drawn for an empty list, so the toolbar's count says so.
    expect(screen.getByTestId("diff-review-file-count")).toHaveTextContent("0 files");
    expect(lineCounts()).toBeDisabled();
    expect(isCommandAvailable(breakdownCommand()!.availability)).toBe(false);
  });
});

describe("find's highlights, requests and hidden matches", () => {
  const FIRST = "src/first.ts";
  const SECOND = "src/second.ts";
  const TEST = "src/first.test.ts";
  // Each file holds "line 150", the one line the mock renders for every item; each its own
  // content, as the review keys its items by it.
  const longFile = (path: string): DiffReviewContent => ({
    status: "ready",
    fileDiff: computeFileDiff({
      path,
      before,
      after: before.replace("line 295\n", "line 295 changed\n"),
      ignoreWhitespace: false,
      revision: "1",
    }),
  });
  const FIRST_CONTENT = longFile(FIRST);
  const SECOND_CONTENT = longFile(SECOND);
  const twoFiles = (overrides: Partial<DiffReviewProps> = {}) =>
    props(ready, {
      files: [
        reviewFile(FIRST, "modified", { additions: 1, deletions: 1 }),
        reviewFile(SECOND, "modified", { additions: 1, deletions: 1 }),
      ],
      contents: new Map([
        [FIRST, FIRST_CONTENT],
        [SECOND, SECOND_CONTENT],
      ]),
      revealPath: undefined,
      findOpen: true,
      ...overrides,
    });
  let highlights: Map<string, FakeHighlight>;
  // Frames run only when a test lets them, so a callback queued for an older request can run late.
  let frames: FrameRequestCallback[];
  const flushFrames = () => {
    for (let round = 0; round < 50 && frames.length > 0; round += 1) {
      for (const frame of frames.splice(0)) frame(0);
    }
  };
  const paintedItem = (name: string) =>
    [...(highlights.get(name)?.ranges ?? [])].map(
      (range) =>
        (range.startContainer.getRootNode() as ShadowRoot).host?.getAttribute?.(
          "data-review-item",
        ) ?? null,
    );
  const query = (text: string) =>
    fireEvent.change(screen.getByTestId("diff-review-find-input"), { target: { value: text } });

  beforeEach(() => {
    highlights = installFakeHighlights();
    frames = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    usePaletteCommandsStore.setState({ groups: {} });
  });

  it("paints a pull request's match under its own names, not the session review's", async () => {
    render(
      <DiffReview
        {...twoFiles()}
        source={{ findHighlightNames: { all: "pr-find", current: "pr-find-active" } }}
      />,
    );
    query("line 150");
    await settle();
    flushFrames();
    expect(paintedItem("pr-find-active")).toHaveLength(1);
    expect(highlights.has("diff-review-find-current")).toBe(false);
  });

  it("paints only the newest request's match when an older one's frames run late", async () => {
    render(<DiffReview {...twoFiles()} />);
    query("line 150");
    await settle();
    // A newer Next before the first paint ran.
    const first = lineScrolls().at(-1)!;
    fireEvent.keyDown(screen.getByTestId("diff-review-find-input"), { key: "Enter" });
    await settle();
    const newest = lineScrolls().at(-1)!;
    expect(newest.id).not.toBe(first.id);
    // Both requests' paints were queued; only the newest one's runs.
    flushFrames();
    expect(paintedItem("diff-review-find-current")).toEqual([newest.id]);
  });

  it.each([
    [
      "the find closes",
      (view: ReturnType<typeof render>) =>
        view.rerender(<DiffReview {...twoFiles({ findOpen: false })} />),
    ],
    ["the query changes to no match", () => query("no such text")],
    [
      "an option leaves no match",
      () => fireEvent.click(screen.getByRole("button", { name: "Match case" })),
    ],
    [
      "the file's text is replaced",
      (view: ReturnType<typeof render>) =>
        view.rerender(
          <DiffReview
            {...twoFiles({
              contents: new Map([
                [FIRST, contentFor(FIRST, "replaced\n")],
                [SECOND, contentFor(SECOND, "replaced\n")],
              ]),
            })}
          />,
        ),
    ],
    [
      // The same paths in the new scope, without the match: only a stale paint could paint.
      "the review's scope changes",
      (view: ReturnType<typeof render>) =>
        view.rerender(
          <DiffReview
            {...twoFiles({
              reviewScope: "another",
              contents: new Map([
                [FIRST, contentFor(FIRST, "replaced\n")],
                [SECOND, contentFor(SECOND, "replaced\n")],
              ]),
            })}
          />,
        ),
    ],
    ["the review unmounts", (view: ReturnType<typeof render>) => view.unmount()],
  ])("paints nothing queued before %s", async (_when, change) => {
    const view = render(<DiffReview {...twoFiles()} />);
    query("LINE 150");
    await settle();
    expect(frames.length).toBeGreaterThan(0);
    await act(async () => change(view));
    await settle();
    flushFrames();
    expect(highlights.has("diff-review-find-current")).toBe(false);
    expect(highlights.has("pr-find-active")).toBe(false);
  });

  it("leaves nothing under the old names once the highlight names change", async () => {
    const view = render(<DiffReview {...twoFiles()} />);
    query("line 150");
    await settle();
    view.rerender(
      <DiffReview
        {...twoFiles()}
        source={{ findHighlightNames: { all: "pr-find", current: "pr-find-active" } }}
      />,
    );
    await settle();
    flushFrames();
    expect(highlights.has("diff-review-find-current")).toBe(false);
  });

  it("repaints the current match after a render, and clears only its own once the row is gone", async () => {
    render(<DiffReview {...twoFiles()} />);
    query("line 150");
    await settle();
    flushFrames();
    expect(paintedItem("diff-review-find-current")).toHaveLength(1);
    const painted = highlights.get("diff-review-find-current")!.ranges[0];
    // Another review's range on the same names stays through this one's repaints.
    const other = {};
    const theirs = document.createRange();
    paintOwnedFindHighlights(other, DIFF_FIND_HIGHLIGHTS, [theirs], theirs);
    const postRender = codeView.props!.options.onPostRender as (...args: unknown[]) => void;
    act(() => postRender());
    flushFrames();
    const after = highlights.get("diff-review-find-current")!.ranges;
    expect(after).toContain(theirs);
    expect(after).toHaveLength(2);
    expect(after).not.toContain(painted);
    clearOwnedFindHighlights(other);
  });

  it("counts matches in the files the filters hide, and Show brings them back from the bar or ⌘K", async () => {
    render(
      <DiffReview
        {...props(ready, {
          files: [reviewFile(TEST, "modified", { additions: 1, deletions: 1 })],
          contents: new Map([[TEST, ready]]),
          revealPath: undefined,
          findOpen: true,
          commandHeading: "Changes",
        })}
      />,
    );
    // Skipping test files leaves nothing listed.
    fireEvent.click(screen.getByRole("button", { name: "Skip test files" }));
    expect(screen.getByTestId("diff-review-file-count")).toHaveTextContent("0 files");
    query("line 150");
    await settle();
    expect(screen.getByText("1 match is in hidden files.")).toBeVisible();
    const show = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === "Show hidden matches");
    expect(isCommandAvailable(show()!.availability)).toBe(true);
    act(() => show()!.run());
    expect(screen.getByTestId("diff-review-file-count")).toHaveTextContent("1 file");
    expect(screen.queryByText(/in hidden files/)).toBeNull();
    // Typing carries on in the find field.
    expect(screen.getByTestId("diff-review-find-input")).toHaveFocus();
    expect(isCommandAvailable(show()!.availability)).toBe(false);
  });

  it("offers Show only while this review's find is open and has hidden matches, and not while hidden", async () => {
    const view = render(
      <DiffReview {...twoFiles({ findOpen: false, commandHeading: "Changes" })} />,
    );
    const show = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === "Show hidden matches");
    expect(show()?.availability).toEqual(
      expect.objectContaining({ available: false, reason: "Find isn't open in this review." }),
    );
    view.rerender(<DiffReview {...twoFiles({ commandHeading: "Changes" })} />);
    expect(show()?.availability).toEqual(
      expect.objectContaining({ available: false, reason: "No find matches are in hidden files." }),
    );
    view.rerender(<DiffReview {...twoFiles({ commandsActive: false })} />);
    expect(show()).toBeUndefined();
  });

  it("names each file's header by the item it renders, so a reused item never keeps an old path", () => {
    render(<DiffReview {...twoFiles({ findOpen: false })} />);
    const header = (index: number) =>
      render(<>{codeView.props!.renderHeaderPrefix!(codeView.props!.items[index]!)}</>);
    expect(header(0).container.querySelector("[data-github-file]")).toHaveAttribute(
      "data-github-file",
      FIRST,
    );
    expect(header(1).container.querySelector("[data-github-file]")).toHaveAttribute(
      "data-github-file",
      SECOND,
    );
  });

  it("is named Find in changes", () => {
    render(<DiffReview {...twoFiles()} />);
    expect(screen.getByRole("search", { name: "Find in changes" })).toBeInTheDocument();
  });
});
