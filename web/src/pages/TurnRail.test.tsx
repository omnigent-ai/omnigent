// Render + interaction invariants for the TurnRail minimap. The rail's
// scroll-positioning (scrollbar-thumb tracking, load-at-bottom, fade edges)
// depends on real layout — offsetTop/clientHeight are 0 in jsdom — so those
// are verified live, not here. These cover the layout-independent contract:
// - < 2 turns → renders nothing (nothing to navigate).
// - one tick (button) per turn, in order, with a jump aria-label.
// - clicking a tick scrolls the transcript to that user message.
// - the whole tick band is the hit target (h-2.5, not just the 2px dash), so a
//   click matches the hover zone.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    // The clickable button is h-2.5 (full pitch) so clicking anywhere in a
    // tick's band navigates — matching the hover zone. A regression to the
    // old h-2 dash-only target would strand clicks in the between-tick gap.
    renderRail(makeTurns(2));
    const tick = screen.getAllByRole("button")[0]!;
    expect(tick).toHaveClass("h-2.5");
  });

  it("highlights only the turn whose content region contains the viewport midpoint", async () => {
    const turns = makeTurns(3);
    const scroller = document.createElement("div");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 100,
    } as DOMRect);

    const anchorTops = [-100, 40, 80];
    const anchors = turns.map((turn, index) => {
      const anchor = document.createElement("div");
      anchor.dataset.userMessageId = turn.itemId;
      vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({
        top: anchorTops[index],
        bottom: anchorTops[index]! + 20,
      } as DOMRect);
      document.body.appendChild(anchor);
      return anchor;
    });

    render(
      <TurnRail
        turns={turns}
        scroller={{ el: scroller }}
        hasMoreHistory={false}
        loadingMoreHistory={false}
      />,
    );

    await waitFor(() => {
      const activeTicks = screen
        .getAllByRole("button")
        .filter((tick) => tick.firstElementChild?.classList.contains("bg-foreground"));
      expect(activeTicks).toHaveLength(1);
      expect(activeTicks[0]).toHaveAccessibleName("Jump to: prompt number 1");
    });

    for (const anchor of anchors) anchor.remove();
  });

  it("keeps the first tick active while the first message is visible", async () => {
    const turns = makeTurns(3);
    const scroller = document.createElement("div");
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 100,
    } as DOMRect);

    const anchorRects = [
      { top: 0, bottom: 20 },
      { top: 40, bottom: 60 },
      { top: 80, bottom: 100 },
    ];
    const anchors = turns.map((turn, index) => {
      const anchor = document.createElement("div");
      anchor.dataset.userMessageId = turn.itemId;
      vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue(anchorRects[index] as DOMRect);
      document.body.appendChild(anchor);
      return anchor;
    });

    render(
      <TurnRail
        turns={turns}
        scroller={{ el: scroller }}
        hasMoreHistory={false}
        loadingMoreHistory={false}
      />,
    );

    await waitFor(() => {
      const activeTicks = screen
        .getAllByRole("button")
        .filter((tick) => tick.firstElementChild?.classList.contains("bg-foreground"));
      expect(activeTicks).toHaveLength(1);
      expect(activeTicks[0]).toHaveAccessibleName("Jump to: prompt number 0");
    });

    for (const anchor of anchors) anchor.remove();
  });

  it("shows the hovered turn's preview when the cursor moves onto a tick", () => {
    renderRail(makeTurns(3));
    const ticks = screen.getAllByRole("button");
    // A genuine hover moves the cursor to a fresh position.
    fireEvent.mouseEnter(ticks[1]!, { clientX: 10, clientY: 20 });
    // The preview box renders the hovered turn's user text.
    expect(screen.getByText("prompt number 1")).toBeInTheDocument();
  });

  it("ignores a mouseenter at the same cursor position (scroll under a still cursor)", () => {
    // Scrolling drags ticks under a stationary cursor, firing mouseenter on
    // each with the SAME clientX/clientY. Those must not swap the preview —
    // only a real cursor move (different position) should.
    renderRail(makeTurns(3));
    const ticks = screen.getAllByRole("button");
    fireEvent.mouseEnter(ticks[0]!, { clientX: 10, clientY: 20 });
    expect(screen.getByText("prompt number 0")).toBeInTheDocument();

    // Same position → the scroll-induced enter on another tick is ignored, so
    // the preview stays on turn 0.
    fireEvent.mouseEnter(ticks[2]!, { clientX: 10, clientY: 20 });
    expect(screen.getByText("prompt number 0")).toBeInTheDocument();
    expect(screen.queryByText("prompt number 2")).not.toBeInTheDocument();

    // A real move (different position) updates the preview again.
    fireEvent.mouseEnter(ticks[2]!, { clientX: 10, clientY: 40 });
    expect(screen.getByText("prompt number 2")).toBeInTheDocument();
  });

  it("shows the preview on keyboard focus and clears it on blur", () => {
    // Keyboard users reach ticks via Tab: onFocus opens the preview, and
    // blurring the tick must close it so tabbing away doesn't strand a stale
    // preview on screen.
    renderRail(makeTurns(3));
    const ticks = screen.getAllByRole("button");
    fireEvent.focus(ticks[1]!);
    expect(screen.getByText("prompt number 1")).toBeInTheDocument();

    fireEvent.blur(ticks[1]!);
    expect(screen.queryByText("prompt number 1")).not.toBeInTheDocument();
  });

  it("a tick's blur doesn't clear a preview owned by another tick", () => {
    // The blur handler only clears when the blurring tick still owns the
    // preview, so a late blur from a previously-focused tick can't wipe the
    // preview a newer focus just opened.
    renderRail(makeTurns(3));
    const ticks = screen.getAllByRole("button");
    fireEvent.focus(ticks[0]!);
    fireEvent.focus(ticks[2]!);
    expect(screen.getByText("prompt number 2")).toBeInTheDocument();

    // Stale blur from the first tick — the preview is now turn 2's, so it stays.
    fireEvent.blur(ticks[0]!);
    expect(screen.getByText("prompt number 2")).toBeInTheDocument();
  });

  it("makes the rail interactive immediately", () => {
    const { container } = render(
      <TurnRail
        turns={makeTurns(3)}
        scroller={null}
        hasMoreHistory={false}
        loadingMoreHistory={false}
      />,
    );
    const rail = container.querySelector(".turn-rail-fade")!;
    expect(rail).toHaveClass("pointer-events-auto");
  });

  it("stays visible and interactive while older history loads", () => {
    const { container } = render(
      <TurnRail
        turns={makeTurns(3)}
        scroller={null}
        hasMoreHistory={true}
        loadingMoreHistory={true}
      />,
    );
    const rail = container.querySelector(".turn-rail-fade")!;
    expect(rail).toHaveClass("pointer-events-auto");
    expect(rail.parentElement).not.toHaveClass("opacity-0");
  });
});
