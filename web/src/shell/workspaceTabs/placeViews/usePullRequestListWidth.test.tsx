import { act, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

import {
  PULL_REQUEST_LIST_DEFAULT_WIDTH,
  PULL_REQUEST_LIST_MAX_WIDTH,
  PULL_REQUEST_LIST_MIN_WIDTH,
  clampPullRequestListWidth,
  resetPullRequestListWidthStoreForTesting,
  usePullRequestListWidth,
} from "./usePullRequestListWidth";

beforeEach(() => resetPullRequestListWidthStoreForTesting());
afterEach(() => localStorage.clear());

/** A width saved by an earlier run of the app, read at its start. */
function savedBeforeLoad(width: number) {
  writePanelSizePreference("pullRequestListWidthPx", width);
  resetPullRequestListWidthStoreForTesting();
}

describe("clampPullRequestListWidth", () => {
  it("keeps the list between its narrowest and widest", () => {
    expect(clampPullRequestListWidth(100, 1600)).toBe(PULL_REQUEST_LIST_MIN_WIDTH);
    expect(clampPullRequestListWidth(400, 1600)).toBe(400);
    // The pull request keeps 40% of a wide tab, so the list may take the other 60%.
    expect(clampPullRequestListWidth(2000, 1600)).toBe(960);
  });

  it("grows with the tab past the width it had before the tab was measured", () => {
    expect(clampPullRequestListWidth(5000, 1000)).toBe(600);
    expect(clampPullRequestListWidth(5000, 3000)).toBe(1800);
    expect(clampPullRequestListWidth(5000, 3000)).toBeGreaterThan(PULL_REQUEST_LIST_MAX_WIDTH);
  });

  it("leaves the pull request its share of a narrower tab", () => {
    expect(clampPullRequestListWidth(500, 700)).toBe(380);
    // Never below the list's narrowest, even when the tab is too tight for both.
    expect(clampPullRequestListWidth(500, 400)).toBe(PULL_REQUEST_LIST_MIN_WIDTH);
  });

  it("treats an unmeasured tab as wide", () => {
    expect(clampPullRequestListWidth(2000, null)).toBe(PULL_REQUEST_LIST_MAX_WIDTH);
    expect(clampPullRequestListWidth(300, 0)).toBe(300);
  });
});

function Harness({ containerWidth, name }: { containerWidth: number; name?: string }) {
  const container = useRef<HTMLDivElement>(null);
  const { width, handleProps } = usePullRequestListWidth(container, containerWidth);
  return (
    <div ref={container}>
      <div {...handleProps} data-testid={name && `${name}-handle`} />
      <output data-testid={name && `${name}-width`}>{width}</output>
    </div>
  );
}

/** The widest the list goes in a 1600 px tab: all but the 40% the pull request keeps. */
const WIDEST_IN_1600 = 960;

const shown = () => Number(screen.getByRole("status").textContent);
const handle = () => screen.getByRole("separator", { name: "Resize the pull request list" });
const shownIn = (name: string) => Number(screen.getByTestId(`${name}-width`).textContent);

describe("usePullRequestListWidth", () => {
  it("starts at the default, then at the width the person last chose", () => {
    const { unmount } = render(<Harness containerWidth={1600} />);
    expect(shown()).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH);
    unmount();
    savedBeforeLoad(420);
    render(<Harness containerWidth={1600} />);
    expect(shown()).toBe(420);
  });

  it("resizes from the keyboard within the limits and remembers it", () => {
    savedBeforeLoad(WIDEST_IN_1600 - 10);
    render(<Harness containerWidth={1600} />);
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    expect(shown()).toBe(WIDEST_IN_1600);
    expect(readPanelSizePreference("pullRequestListWidthPx")).toBe(WIDEST_IN_1600);
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    expect(shown()).toBe(WIDEST_IN_1600 - 20);
    expect(handle()).toHaveAttribute("aria-valuenow", String(WIDEST_IN_1600 - 20));
  });

  it("drags from the list's left edge and saves the width on release", () => {
    render(<Harness containerWidth={1600} />);
    fireEvent.pointerDown(handle());
    act(() => {
      window.dispatchEvent(new PointerEvent("pointermove", { clientX: 450 }));
    });
    expect(shown()).toBe(450);
    expect(readPanelSizePreference("pullRequestListWidthPx")).toBeNull();
    act(() => {
      window.dispatchEvent(new PointerEvent("pointermove", { clientX: 5000 }));
      window.dispatchEvent(new PointerEvent("pointerup"));
    });
    expect(shown()).toBe(WIDEST_IN_1600);
    expect(readPanelSizePreference("pullRequestListWidthPx")).toBe(WIDEST_IN_1600);
  });

  it("shows less in a narrow tab and returns to the choice when there's room", () => {
    savedBeforeLoad(480);
    const { rerender } = render(<Harness containerWidth={700} />);
    expect(shown()).toBe(380);
    rerender(<Harness containerWidth={1600} />);
    expect(shown()).toBe(480);
  });

  it("double-click resets to the default", () => {
    savedBeforeLoad(480);
    render(<Harness containerWidth={1600} />);
    fireEvent.doubleClick(handle());
    expect(shown()).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH);
  });

  it("resets to the default in a tight tab and widens to it when there's room", () => {
    savedBeforeLoad(480);
    const { rerender } = render(<Harness containerWidth={600} />);
    expect(shown()).toBe(280);
    fireEvent.doubleClick(handle());
    expect(readPanelSizePreference("pullRequestListWidthPx")).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH);
    rerender(<Harness containerWidth={1600} />);
    expect(shown()).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH);
  });

  it("shows a width chosen in one tab in the others already open", () => {
    render(
      <>
        <Harness containerWidth={1600} name="first" />
        <Harness containerWidth={1600} name="second" />
      </>,
    );
    fireEvent.keyDown(screen.getByTestId("first-handle"), { key: "ArrowRight" });
    expect(shownIn("first")).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH + 20);
    expect(shownIn("second")).toBe(PULL_REQUEST_LIST_DEFAULT_WIDTH + 20);
  });
});
