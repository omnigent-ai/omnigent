// Render + interaction invariants for the TurnRail minimap. The rail's
// scroll-positioning (scrollbar-thumb tracking, load-at-bottom, fade edges)
// depends on real layout — offsetTop/clientHeight are 0 in jsdom — so those
// are verified live, not here. These cover the layout-independent contract:
// - < 2 turns → renders nothing (nothing to navigate).
// - one tick (button) per turn, in order, with a jump aria-label.
// - clicking a tick scrolls the transcript to that user message.
// - the whole tick band is the hit target (h-4, not just the 2px dash), so a
//   click matches the hover zone.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TurnRail, type Turn } from "./TurnRail";

// The rail calls scrollToUserMessage on click; stub it so we assert the call
// without needing a real scroll container / DOM anchors.
const scrollSpy = vi.fn();
vi.mock("@/hooks/useUserMessageNav", () => ({
  scrollToUserMessage: (...args: unknown[]) => scrollSpy(...args),
}));

function makeTurns(n: number): Turn[] {
  return Array.from({ length: n }, (_, i) => ({
    itemId: `turn_${i}`,
    userText: `prompt number ${i}`,
    responsePreview: `reply preview ${i}`,
  }));
}

function renderRail(turns: Turn[]) {
  return render(
    <TurnRail turns={turns} scroller={null} hasMoreHistory={false} loadingMoreHistory={false} />,
  );
}

afterEach(() => {
  cleanup();
  scrollSpy.mockReset();
});

describe("TurnRail", () => {
  it("renders nothing for a single-turn (or empty) conversation", () => {
    const { container } = renderRail(makeTurns(1));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders one tick per turn once there are at least two", () => {
    renderRail(makeTurns(4));
    const ticks = screen.getAllByRole("button");
    expect(ticks).toHaveLength(4);
  });

  it("labels each tick with its user text for jump-to affordance", () => {
    renderRail(makeTurns(3));
    expect(screen.getByLabelText("Jump to: prompt number 0")).toBeInTheDocument();
    expect(screen.getByLabelText("Jump to: prompt number 2")).toBeInTheDocument();
  });

  it("scrolls the transcript to the clicked turn's message", () => {
    renderRail(makeTurns(3));
    fireEvent.click(screen.getByLabelText("Jump to: prompt number 1"));
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(scrollSpy.mock.calls[0]![0]).toBe("turn_1");
  });

  it("gives each tick a full-height hit band, not just the dash", () => {
    // The clickable button is h-4 (full pitch) so clicking anywhere in a
    // tick's band navigates — matching the hover zone. A regression to the
    // old h-2 dash-only target would strand clicks in the between-tick gap.
    renderRail(makeTurns(2));
    const tick = screen.getAllByRole("button")[0]!;
    expect(tick).toHaveClass("h-4");
  });
});
