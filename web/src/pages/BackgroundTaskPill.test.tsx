// Tests for BackgroundTaskPill — the "N background task(s)" pill shown above
// the composer while background shells outlive a turn. We mock the store so
// the count/self-hide logic and the overlay layout are exercised in isolation.
//
// The overlay layout matters: the pill floats over the transcript's bottom
// (absolute + bottom-full) rather than taking a flow row. A flow row butts
// against the transcript's overflow edge and clips its last line — the bug this
// covers — so these tests pin the classes that keep it an overlay.

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
    // WHY: the pill must occupy no space (and cast no overlay) when idle, so it
    // self-gates to null rather than rendering an empty container.
    h.count = 0;
    const { container } = render(<BackgroundTaskPill />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the singular count", () => {
    // Scope to the visible pill: an invisible spacer mirrors the same label to
    // reserve the collapsed footprint, so the text appears twice in the DOM.
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

  it("floats as an overlay pinned above the composer, not a flow row", () => {
    // WHY: the pill must NOT reserve a flow row. A reserved row sits against the
    // transcript's bottom overflow edge and clips its last line (the reported
    // bug). The fix pins it as an overlay (absolute + bottom-full) that is
    // pointer-transparent except for the pill itself, so the transcript beneath
    // stays visible and scrollable.
    h.count = 2;
    const { container } = render(<BackgroundTaskPill />);
    const overlay = container.firstElementChild as HTMLElement;
    expect(overlay).toHaveClass("absolute");
    expect(overlay).toHaveClass("bottom-full");
    expect(overlay).toHaveClass("pointer-events-none");
  });

  it("re-enables pointer events on the pill itself so it stays interactive", () => {
    // WHY: the overlay is pointer-transparent (so the transcript beneath stays
    // clickable), but the pill must still take hover/tap to expand — it opts
    // back in with pointer-events-auto.
    h.count = 1;
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    // The pill's positioned wrapper re-enables pointer events.
    expect(pill.parentElement).toHaveClass("pointer-events-auto");
  });

  it("keeps a plain tally (not expandable) when no per-shell detail is present", () => {
    // WHY: an older runner reports only the count with no `backgroundTasks`
    // detail; the pill must stay a non-focusable tally rather than advertising
    // an empty expandable card.
    h.count = 2;
    h.tasks = [];
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    expect(pill).not.toHaveAttribute("tabindex");
  });

  it("becomes focusable/expandable when per-shell detail is present", () => {
    // WHY: with per-shell detail the pill expands into a card listing each
    // shell, so it must be reachable by keyboard (tabIndex 0) and focus-open.
    h.count = 1;
    h.tasks = [{ id: "s1", description: "Wait for CI" }];
    const pill = render(<BackgroundTaskPill />).getByTestId("background-task-pill");
    expect(pill).toHaveAttribute("tabindex", "0");
  });
});
