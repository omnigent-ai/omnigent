// Tests for BackgroundTaskPill — the "N background task(s)" tally shown in
// the workspace bar's right-side running-work slot while background shells
// outlive a turn. We mock the store so the count/self-hide logic and the
// in-bar layout are exercised in isolation.
//
// The layout matters: the tally sits in the bar's flow (a relative wrapper
// reserving its collapsed footprint) instead of floating as an overlay above
// the bar — the floating chip was the bug — and its morphing card anchors to
// the wrapper's bottom-right so expansion grows up-left without shifting the
// bar row.

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BackgroundTaskInfo } from "@/lib/types";

interface StoreShape {
  backgroundTaskCount: number;
  backgroundTasks: BackgroundTaskInfo[];
}

const h = vi.hoisted(() => ({
  count: 0,
  tasks: [] as BackgroundTaskInfo[],
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: StoreShape) => unknown) =>
    selector({ backgroundTaskCount: h.count, backgroundTasks: h.tasks }),
}));

import { BackgroundTaskPill } from "./ChatPage";

afterEach(() => {
  cleanup();
  h.count = 0;
  h.tasks = [];
});

describe("BackgroundTaskPill", () => {
  it("renders nothing when no background tasks are running", () => {
    // WHY: the tally must occupy no space in the bar's right slot when idle,
    // so it self-gates to null rather than rendering an empty container.
    h.count = 0;
    const { container } = render(<BackgroundTaskPill />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the singular count", () => {
    // Scope to the visible tally: an invisible spacer mirrors the same label
    // to reserve the collapsed footprint, so the text appears twice in the DOM.
    h.count = 1;
    render(<BackgroundTaskPill />);
    const pill = screen.getByTestId("background-task-pill");
    expect(within(pill).getByText("1 background task")).toBeInTheDocument();
  });

  it("pluralizes the count", () => {
    h.count = 3;
    render(<BackgroundTaskPill />);
    const pill = screen.getByTestId("background-task-pill");
    expect(within(pill).getByText("3 background tasks")).toBeInTheDocument();
  });

  it("sits in the bar's flow instead of floating as an overlay above it", () => {
    // WHY: the reported bug was a standalone chip floating above the gray
    // workspace bar. The tally now takes a flow slot inside the bar (a
    // relative wrapper reserving its footprint), not an absolute overlay
    // pinned above the composer.
    h.count = 2;
    const { container } = render(<BackgroundTaskPill />);
    const wrapper = container.firstElementChild as HTMLElement;
    expect(wrapper).toHaveClass("relative");
    expect(wrapper).not.toHaveClass("absolute");
    expect(wrapper).not.toHaveClass("bottom-full");
    expect(wrapper).not.toHaveClass("pointer-events-none");
  });

  it("anchors the tally bottom-right so the card expands up-left", () => {
    // WHY: the morphing card must grow away from the bar's right edge (up and
    // to the left) — a left anchor would push the expanded card outside the
    // bar and off-screen on narrow viewports.
    h.count = 1;
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    expect(pill).toHaveClass("absolute");
    expect(pill).toHaveClass("right-0");
    expect(pill).toHaveClass("bottom-0");
  });

  it("keeps a plain tally (not expandable) when no per-shell detail is present", () => {
    // WHY: an older runner reports only the count with no `backgroundTasks`
    // detail; the tally must stay a non-focusable count rather than
    // advertising an empty expandable card.
    h.count = 2;
    h.tasks = [];
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    expect(pill).not.toHaveAttribute("tabindex");
  });

  it("becomes focusable/expandable when per-shell detail is present", () => {
    // WHY: with per-shell detail the tally expands into a card listing each
    // shell, so it must be reachable by keyboard (tabIndex 0) and focus-open.
    h.count = 1;
    h.tasks = [{ id: "s1", description: "Wait for CI" }];
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    expect(pill).toHaveAttribute("tabindex", "0");
  });
});
