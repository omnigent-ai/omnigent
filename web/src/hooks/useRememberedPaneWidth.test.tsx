import { fireEvent, render, screen } from "@testing-library/react";
import { StrictMode, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CHANGES_REVIEW_TREE_WIDTH,
  PULL_REQUEST_REVIEW_TREE_WIDTH,
} from "@/components/diff-review/reviewTreeWidth";
import {
  readPanelSharePreference,
  readPanelSizePreference,
  writePanelSharePreference,
  writePanelSizePreference,
} from "@/lib/panelSizePreferences";
import {
  FILES_TREE_DEFAULT_WIDTH,
  FILES_TREE_MAX_WIDTH,
  FILES_TREE_MIN_WIDTH,
  FILES_TREE_WIDTH,
} from "@/shell/workspaceTabs/placeViews/filesTreeWidth";

import {
  clampPaneWidth,
  resetRememberedPaneWidthForTesting,
  useRememberedPaneWidth,
  type PaneWidthSpec,
} from "./useRememberedPaneWidth";

const SIDE_PANEL: PaneWidthSpec = {
  preference: "commentsPanelWidthPx",
  defaultWidth: 320,
  minWidth: 240,
  maxWidth: 520,
  roomIn: (containerWidth) => containerWidth - 320,
  label: "Resize the side panel",
  keyboardStep: 20,
};

// A left pane remembered in pixels, as the Files tree was before it followed its page.
const PIXEL_TREE: PaneWidthSpec = {
  preference: "filesTreeWidthPx",
  defaultWidth: FILES_TREE_DEFAULT_WIDTH,
  minWidth: FILES_TREE_MIN_WIDTH,
  maxWidth: FILES_TREE_MAX_WIDTH,
  roomIn: (pageWidth) => Math.floor(pageWidth * 0.48),
  label: "Resize the file tree",
  keyboardStep: 16,
};
const SPECS = [
  PIXEL_TREE,
  SIDE_PANEL,
  FILES_TREE_WIDTH,
  CHANGES_REVIEW_TREE_WIDTH,
  PULL_REQUEST_REVIEW_TREE_WIDTH,
];
const LEGACY_CHANGES_KEY = "omnigent:changes-review-tree-width";

beforeEach(() => {
  for (const spec of SPECS) resetRememberedPaneWidthForTesting(spec);
});
afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});

function Pane({ spec, containerWidth }: { spec: PaneWidthSpec; containerWidth: number | null }) {
  const container = useRef<HTMLDivElement>(null);
  const { width, handleProps } = useRememberedPaneWidth(spec, container, containerWidth);
  return (
    <div ref={container}>
      <div {...handleProps} />
      <output aria-label={spec.label}>{width}</output>
    </div>
  );
}

const widthOf = (spec: PaneWidthSpec) =>
  Number(screen.getByRole("status", { name: spec.label }).textContent);
const handleOf = (spec: PaneWidthSpec) => screen.getByRole("separator", { name: spec.label });

describe("clampPaneWidth for the Files tree", () => {
  it("keeps the tree above its narrowest and under half the page, however wide", () => {
    expect(clampPaneWidth(FILES_TREE_WIDTH, 100, 1600)).toBe(FILES_TREE_MIN_WIDTH);
    // 48% of a 1600px page: a full-size page's room alone limits the tree.
    expect(clampPaneWidth(FILES_TREE_WIDTH, 2000, 1600)).toBe(768);
    // Before the page is measured, the tree stops at its widest.
    expect(clampPaneWidth(FILES_TREE_WIDTH, 2000, null)).toBe(FILES_TREE_MAX_WIDTH);
    // 48% of a 600px page.
    expect(clampPaneWidth(FILES_TREE_WIDTH, 360, 600)).toBe(288);
    expect(clampPaneWidth(FILES_TREE_WIDTH, 360, 200)).toBe(FILES_TREE_MIN_WIDTH);
  });
});

describe("useRememberedPaneWidth", () => {
  it("keeps each kind of pane's choice apart", () => {
    render(
      <>
        <Pane spec={PIXEL_TREE} containerWidth={1600} />
        <Pane spec={SIDE_PANEL} containerWidth={1600} />
      </>,
    );
    fireEvent.keyDown(handleOf(PIXEL_TREE), { key: "ArrowRight" });

    expect(widthOf(PIXEL_TREE)).toBe(FILES_TREE_DEFAULT_WIDTH + 16);
    expect(widthOf(SIDE_PANEL)).toBe(320);
    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(FILES_TREE_DEFAULT_WIDTH + 16);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
  });

  it("shows less in a narrow page without forgetting the choice, and restores it", () => {
    writePanelSizePreference("filesTreeWidthPx", 340);
    resetRememberedPaneWidthForTesting(PIXEL_TREE);
    const view = render(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    expect(widthOf(PIXEL_TREE)).toBe(340);

    view.rerender(<Pane spec={PIXEL_TREE} containerWidth={500} />);
    expect(widthOf(PIXEL_TREE)).toBe(240);
    expect(handleOf(PIXEL_TREE)).toHaveAttribute("aria-valuemax", "240");
    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(340);

    view.rerender(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    expect(widthOf(PIXEL_TREE)).toBe(340);
  });

  it("resets to the default from a tight page and shows all of it once there's room", () => {
    writePanelSizePreference("filesTreeWidthPx", 340);
    resetRememberedPaneWidthForTesting(PIXEL_TREE);
    const view = render(<Pane spec={PIXEL_TREE} containerWidth={400} />);
    fireEvent.doubleClick(handleOf(PIXEL_TREE));

    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(FILES_TREE_DEFAULT_WIDTH);
    expect(widthOf(PIXEL_TREE)).toBe(192);
    view.rerender(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    expect(widthOf(PIXEL_TREE)).toBe(FILES_TREE_DEFAULT_WIDTH);
  });

  it("saves where the last move left it when the release follows at once", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 100,
    } as DOMRect);
    render(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    fireEvent.pointerDown(handleOf(PIXEL_TREE));
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.pointerMove(window, { clientX: 400 });
    fireEvent.pointerUp(window);

    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(300);
    expect(widthOf(PIXEL_TREE)).toBe(300);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("lets go of the drag when the pane goes away mid-drag", () => {
    const view = render(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    fireEvent.pointerDown(handleOf(PIXEL_TREE));
    expect(document.body.style.userSelect).toBe("none");
    view.unmount();

    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    fireEvent.pointerMove(window, { clientX: 400 });
    expect(readPanelSizePreference("filesTreeWidthPx")).toBeNull();
  });

  it("names the handle and reports its range for assistive technology", () => {
    render(<Pane spec={PIXEL_TREE} containerWidth={1600} />);
    const handle = handleOf(PIXEL_TREE);
    expect(handle).toHaveAttribute("tabindex", "0");
    expect(handle).toHaveAttribute("aria-orientation", "vertical");
    expect(handle).toHaveAttribute("aria-valuemin", String(FILES_TREE_MIN_WIDTH));
    expect(handle).toHaveAttribute("aria-valuemax", String(FILES_TREE_MAX_WIDTH));
    expect(handle).toHaveAttribute("aria-valuenow", String(FILES_TREE_DEFAULT_WIDTH));
  });
});

describe("a pane at the right of its container", () => {
  const RIGHT_PANE: PaneWidthSpec = { ...SIDE_PANEL, anchor: "right" };

  it("widens with ArrowLeft and narrows with ArrowRight, within its limits", () => {
    render(<Pane spec={RIGHT_PANE} containerWidth={1600} />);
    fireEvent.keyDown(handleOf(RIGHT_PANE), { key: "ArrowLeft" });
    expect(widthOf(RIGHT_PANE)).toBe(340);
    fireEvent.keyDown(handleOf(RIGHT_PANE), { key: "ArrowRight" });
    fireEvent.keyDown(handleOf(RIGHT_PANE), { key: "ArrowRight" });
    expect(widthOf(RIGHT_PANE)).toBe(300);
    for (let step = 0; step < 20; step += 1) {
      fireEvent.keyDown(handleOf(RIGHT_PANE), { key: "ArrowRight" });
    }
    expect(widthOf(RIGHT_PANE)).toBe(240);
  });

  it("follows a drag measured from the container's right edge", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 100,
      right: 1700,
      top: 0,
      bottom: 800,
      width: 1600,
      height: 800,
      x: 100,
      y: 0,
      toJSON: () => ({}),
    });
    render(<Pane spec={RIGHT_PANE} containerWidth={1600} />);
    fireEvent.pointerDown(handleOf(RIGHT_PANE));
    fireEvent.pointerMove(window, { clientX: 1300 });
    fireEvent.pointerUp(window);
    expect(widthOf(RIGHT_PANE)).toBe(400);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(400);
  });
});

describe("a pane that collapses when dragged past its edge", () => {
  const collapses = vi.fn();
  const COLLAPSIBLE: PaneWidthSpec = {
    ...SIDE_PANEL,
    anchor: "right",
    collapse: { below: 180, onCollapse: () => collapses() },
  };
  // The container's right edge; a pointer at x is a pane `1000 - x` wide.
  const dragTo = (width: number) => fireEvent.pointerMove(window, { clientX: 1000 - width });
  beforeEach(() => {
    collapses.mockReset();
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0,
      right: 1000,
    } as DOMRect);
  });

  it("collapses on a release past the edge, keeping the width from before the drag", () => {
    writePanelSizePreference("commentsPanelWidthPx", 400);
    resetRememberedPaneWidthForTesting(SIDE_PANEL);
    render(<Pane spec={COLLAPSIBLE} containerWidth={1600} />);
    // The drag ends, cursor and listeners included, before the pane hears it should go.
    collapses.mockImplementation(() => {
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    });
    fireEvent.pointerDown(handleOf(COLLAPSIBLE));
    dragTo(300);
    dragTo(100);
    fireEvent.pointerUp(window);

    expect(collapses).toHaveBeenCalledTimes(1);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(400);
    expect(widthOf(COLLAPSIBLE)).toBe(400);
    fireEvent.pointerMove(window, { clientX: 700 });
    expect(widthOf(COLLAPSIBLE)).toBe(400);
  });

  it("keeps a choice the page squeezes, so a wider page shows all of it again", () => {
    writePanelSizePreference("commentsPanelWidthPx", 400);
    resetRememberedPaneWidthForTesting(SIDE_PANEL);
    // Room for 300 of the 400 chosen.
    const { rerender } = render(<Pane spec={COLLAPSIBLE} containerWidth={620} />);
    expect(widthOf(COLLAPSIBLE)).toBe(300);
    fireEvent.pointerDown(handleOf(COLLAPSIBLE));
    dragTo(100);
    fireEvent.pointerUp(window);

    expect(collapses).toHaveBeenCalledTimes(1);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(400);
    rerender(<Pane spec={COLLAPSIBLE} containerWidth={1600} />);
    expect(widthOf(COLLAPSIBLE)).toBe(400);
  });

  it("leaves no choice saved when there was none, so the default still applies", () => {
    render(<Pane spec={COLLAPSIBLE} containerWidth={1600} />);
    fireEvent.pointerDown(handleOf(COLLAPSIBLE));
    dragTo(260);
    dragTo(100);
    fireEvent.pointerUp(window);

    expect(collapses).toHaveBeenCalledTimes(1);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    expect(widthOf(COLLAPSIBLE)).toBe(320);
  });

  it("resizes as usual when the drag comes back from past the edge before the release", () => {
    render(<Pane spec={COLLAPSIBLE} containerWidth={1600} />);
    fireEvent.pointerDown(handleOf(COLLAPSIBLE));
    dragTo(100);
    dragTo(300);
    fireEvent.pointerUp(window);

    expect(collapses).not.toHaveBeenCalled();
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(300);
  });

  it("keeps double-click reset, and keyboard resizing stops at its narrowest", () => {
    writePanelSizePreference("commentsPanelWidthPx", 260);
    resetRememberedPaneWidthForTesting(SIDE_PANEL);
    render(<Pane spec={COLLAPSIBLE} containerWidth={1600} />);
    const handle = handleOf(COLLAPSIBLE);
    expect(handle).toHaveAttribute(
      "title",
      "Drag to resize, or past the edge to collapse. Double-click to reset.",
    );
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(widthOf(COLLAPSIBLE)).toBe(240);
    expect(collapses).not.toHaveBeenCalled();
    fireEvent.doubleClick(handle);
    expect(widthOf(COLLAPSIBLE)).toBe(320);
  });

  it("leaves a pane without the option resizing and resetting as before", () => {
    render(<Pane spec={{ ...SIDE_PANEL, anchor: "right" }} containerWidth={1600} />);
    const handle = handleOf(SIDE_PANEL);
    expect(handle).toHaveAttribute("title", "Drag to resize. Double-click to reset.");
    fireEvent.pointerDown(handle);
    dragTo(100);
    fireEvent.pointerUp(window);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(240);
    fireEvent.doubleClick(handle);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(320);
  });
});

describe("a tree that keeps its share of the panel", () => {
  const FILES = FILES_TREE_WIDTH;

  it("grows and shrinks with its page from the default, within its limits", () => {
    // The default fits the narrowest page that docks the tree.
    const view = render(<Pane spec={FILES} containerWidth={720} />);
    expect(widthOf(FILES)).toBe(FILES_TREE_DEFAULT_WIDTH);
    view.rerender(<Pane spec={FILES} containerWidth={900} />);
    expect(widthOf(FILES)).toBe(320);
    // A fractional page width rounds to whole pixels.
    view.rerender(<Pane spec={FILES} containerWidth={777.5} />);
    expect(widthOf(FILES)).toBe(276);
    // A full-size page widens it in proportion.
    view.rerender(<Pane spec={FILES} containerWidth={1400} />);
    expect(widthOf(FILES)).toBe(498);
    view.rerender(<Pane spec={FILES} containerWidth={720} />);
    expect(widthOf(FILES)).toBe(FILES_TREE_DEFAULT_WIDTH);
    // Following the page is not a choice.
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
  });

  it("keeps its minimum when half the page is narrower, and recovers its share once wide", () => {
    writePanelSharePreference("filesTreeShare", 0.4);
    resetRememberedPaneWidthForTesting(FILES);
    const view = render(<Pane spec={FILES} containerWidth={300} />);
    expect(widthOf(FILES)).toBe(FILES_TREE_MIN_WIDTH);
    // 48% of 600 caps the 240 the share asks for.
    view.rerender(<Pane spec={FILES} containerWidth={600} />);
    expect(widthOf(FILES)).toBe(240);
    view.rerender(<Pane spec={FILES} containerWidth={800} />);
    expect(widthOf(FILES)).toBe(320);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.4);
  });

  it("remembers a key step as its share, so a wider page widens the choice", () => {
    const view = render(<Pane spec={FILES} containerWidth={800} />);
    expect(widthOf(FILES)).toBe(284);
    fireEvent.keyDown(handleOf(FILES), { key: "ArrowRight" });
    expect(widthOf(FILES)).toBe(300);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.375);
    view.rerender(<Pane spec={FILES} containerWidth={640} />);
    expect(widthOf(FILES)).toBe(240);
    view.rerender(<Pane spec={FILES} containerWidth={1600} />);
    expect(widthOf(FILES)).toBe(600);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.375);
  });

  it("remembers a drag as its share on release, and double-click returns to the default", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 100,
    } as DOMRect);
    const view = render(<Pane spec={FILES} containerWidth={1000} />);
    fireEvent.pointerDown(handleOf(FILES));
    fireEvent.pointerMove(window, { clientX: 400 });
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    fireEvent.pointerUp(window);
    expect(widthOf(FILES)).toBe(300);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.3);

    view.rerender(<Pane spec={FILES} containerWidth={900} />);
    expect(widthOf(FILES)).toBe(270);
    fireEvent.doubleClick(handleOf(FILES));
    expect(widthOf(FILES)).toBe(320);
    expect(readPanelSharePreference("filesTreeShare")).toBeCloseTo(256 / 720);
  });

  it("turns a width chosen in pixels into its share without moving the tree", () => {
    writePanelSizePreference("filesTreeWidthPx", 300);
    resetRememberedPaneWidthForTesting(FILES);
    // Unmeasured, it draws the old width and converts nothing.
    const view = render(<Pane spec={FILES} containerWidth={null} />);
    expect(widthOf(FILES)).toBe(300);
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();

    view.rerender(<Pane spec={FILES} containerWidth={1000} />);
    expect(widthOf(FILES)).toBe(300);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.3);
    view.rerender(<Pane spec={FILES} containerWidth={900} />);
    expect(widthOf(FILES)).toBe(270);
    // The old value stays where it was; the share now wins.
    expect(readPanelSizePreference("filesTreeWidthPx")).toBe(300);
  });

  it("converts an old width past the limits as the tree drew it", () => {
    writePanelSizePreference("filesTreeWidthPx", 9000);
    resetRememberedPaneWidthForTesting(FILES);
    render(<Pane spec={FILES} containerWidth={800} />);
    // 48% of the page is as wide as it draws.
    expect(widthOf(FILES)).toBe(384);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.48);
  });

  it("prefers a remembered share over an old pixel width", () => {
    writePanelSizePreference("filesTreeWidthPx", 340);
    writePanelSharePreference("filesTreeShare", 0.25);
    resetRememberedPaneWidthForTesting(FILES);
    render(<Pane spec={FILES} containerWidth={800} />);
    expect(widthOf(FILES)).toBe(200);
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.25);
  });

  it("converts once for every mounted tree, in Strict Mode too", () => {
    writePanelSizePreference("filesTreeWidthPx", 300);
    resetRememberedPaneWidthForTesting(FILES);
    const writes = vi.spyOn(Storage.prototype, "setItem");
    render(
      <StrictMode>
        <Pane spec={FILES} containerWidth={1000} />
        <Pane spec={FILES} containerWidth={1000} />
      </StrictMode>,
    );
    expect(
      screen.getAllByRole("status", { name: FILES.label }).map((node) => node.textContent),
    ).toEqual(["300", "300"]);
    expect(writes).toHaveBeenCalledTimes(1);
  });

  it("follows its page in memory when storage is blocked", () => {
    writePanelSizePreference("filesTreeWidthPx", 300);
    resetRememberedPaneWidthForTesting(FILES);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const view = render(<Pane spec={FILES} containerWidth={1000} />);
    expect(widthOf(FILES)).toBe(300);
    view.rerender(<Pane spec={FILES} containerWidth={900} />);
    expect(widthOf(FILES)).toBe(270);
    fireEvent.keyDown(handleOf(FILES), { key: "ArrowRight" });
    expect(widthOf(FILES)).toBe(286);
  });
});

describe("the trees beside a Changes review", () => {
  it("convert the session review's old pixel width, and ignore an invalid one", () => {
    localStorage.setItem(LEGACY_CHANGES_KEY, "300");
    resetRememberedPaneWidthForTesting(CHANGES_REVIEW_TREE_WIDTH);
    const view = render(<Pane spec={CHANGES_REVIEW_TREE_WIDTH} containerWidth={1000} />);
    expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(300);
    expect(readPanelSharePreference("changesReviewTreeShare")).toBe(0.3);
    view.unmount();

    for (const stored of ["wide", "-5", "0"]) {
      localStorage.clear();
      localStorage.setItem(LEGACY_CHANGES_KEY, stored);
      resetRememberedPaneWidthForTesting(CHANGES_REVIEW_TREE_WIDTH);
      const again = render(<Pane spec={CHANGES_REVIEW_TREE_WIDTH} containerWidth={720} />);
      expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(228);
      again.unmount();
    }
  });

  it("step by 12, by 32 with Shift, and jump to their limits with Home and End", () => {
    render(<Pane spec={CHANGES_REVIEW_TREE_WIDTH} containerWidth={720} />);
    const handle = handleOf(CHANGES_REVIEW_TREE_WIDTH);
    expect(handle).toHaveAttribute("aria-valuemin", "180");
    expect(handle).toHaveAttribute("aria-valuemax", "345");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(240);
    fireEvent.keyDown(handle, { key: "ArrowLeft", shiftKey: true });
    expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(208);
    fireEvent.keyDown(handle, { key: "End" });
    expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(345);
    fireEvent.keyDown(handle, { key: "Home" });
    expect(widthOf(CHANGES_REVIEW_TREE_WIDTH)).toBe(180);
    expect(readPanelSharePreference("changesReviewTreeShare")).toBe(0.25);
    expect(fireEvent.keyDown(handle, { key: "a" })).toBe(true);
  });

  it("keep the session's and the pull request's choices apart", () => {
    render(
      <>
        <Pane spec={CHANGES_REVIEW_TREE_WIDTH} containerWidth={1000} />
        <Pane spec={PULL_REQUEST_REVIEW_TREE_WIDTH} containerWidth={1000} />
      </>,
    );
    // Both are labelled "Resize changed files"; the pull request's comes second.
    const [, pullRequest] = screen.getAllByRole("separator", { name: "Resize changed files" });
    expect(pullRequest).toHaveAttribute("aria-valuemin", "160");
    fireEvent.keyDown(pullRequest, { key: "End" });
    const widths = screen
      .getAllByRole("status", { name: "Resize changed files" })
      .map((node) => node.textContent);
    // 228/720 and 240/720 of 1000, then the pull request's at 48% of it.
    expect(widths).toEqual(["317", "480"]);
    expect(readPanelSharePreference("pullRequestReviewTreeShare")).toBe(0.48);
    expect(readPanelSharePreference("changesReviewTreeShare")).toBeNull();
  });

  it("end a cancelled drag where it was, and give back the page's cursor", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0,
    } as DOMRect);
    document.body.style.cursor = "progress";
    render(<Pane spec={PULL_REQUEST_REVIEW_TREE_WIDTH} containerWidth={1000} />);
    fireEvent.pointerDown(handleOf(PULL_REQUEST_REVIEW_TREE_WIDTH));
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.pointerMove(window, { clientX: 400 });
    fireEvent.pointerCancel(window);

    expect(document.body.style.cursor).toBe("progress");
    expect(document.body.style.userSelect).toBe("");
    expect(readPanelSharePreference("pullRequestReviewTreeShare")).toBe(0.4);
    fireEvent.pointerMove(window, { clientX: 300 });
    expect(widthOf(PULL_REQUEST_REVIEW_TREE_WIDTH)).toBe(400);
  });
});
