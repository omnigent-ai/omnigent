import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TabStripScroller } from "./TabStripScroller";

/** Lay the strip out by hand, since jsdom has no layout: 60px tabs in a 100px viewport by default. */
function layOut(viewport: HTMLElement, viewportWidth = 100) {
  const rect = (left: number, width: number) =>
    ({
      left,
      right: left + width,
      width,
      top: 0,
      bottom: 20,
      height: 20,
      x: left,
      y: 0,
    }) as DOMRect;
  viewport.getBoundingClientRect = () => rect(0, viewportWidth);
  screen.getAllByRole("tab").forEach((tab, index) => {
    tab.getBoundingClientRect = () => rect(index * 60 - viewport.scrollLeft, 60);
  });
}

function Strip({ selected, fadeEdges }: { selected: number; fadeEdges?: boolean }) {
  return (
    <TabStripScroller data-testid="strip" fadeEdges={fadeEdges}>
      {["Summary", "Comments", "Changes"].map((label, index) => (
        <button key={label} type="button" role="tab" aria-selected={index === selected}>
          {label}
        </button>
      ))}
    </TabStripScroller>
  );
}

afterEach(cleanup);

describe("TabStripScroller", () => {
  it("scrolls only itself to bring a newly selected tab into view, either way", async () => {
    const { rerender } = render(<Strip selected={0} />);
    const viewport = screen.getByTestId("strip");
    layOut(viewport);
    const outer = viewport.parentElement!;
    outer.scrollLeft = 0;

    rerender(<Strip selected={2} />);
    await act(async () => {});
    // Changes spans 120–180, so the strip scrolls 80px to end at its right edge.
    expect(viewport.scrollLeft).toBe(80);
    expect(outer.scrollLeft).toBe(0);

    rerender(<Strip selected={0} />);
    await act(async () => {});
    expect(viewport.scrollLeft).toBe(0);
  });

  it("reveals a tab's whole entry, close button included, when its button fits but the entry doesn't", async () => {
    // Entries are 80px with a 60px button; the viewport is 150px, so the second
    // entry (80–160) overflows by 10px while its button (80–140) fits.
    function Entries({ selected }: { selected: number }) {
      return (
        <TabStripScroller data-testid="entries">
          {[0, 1].map((index) => (
            <div key={index} data-tab-strip-entry data-testid={`entry-${index}`}>
              <button type="button" role="tab" aria-selected={index === selected}>
                Tab {index}
              </button>
              <button type="button" tabIndex={-1}>
                ×
              </button>
            </div>
          ))}
        </TabStripScroller>
      );
    }
    const box = (left: number, width: number) =>
      ({
        left,
        right: left + width,
        width,
        top: 0,
        bottom: 20,
        height: 20,
        x: left,
        y: 0,
      }) as DOMRect;
    const { rerender } = render(<Entries selected={0} />);
    const viewport = screen.getByTestId("entries");
    viewport.getBoundingClientRect = () => box(0, 150);
    [0, 1].forEach((index) => {
      screen.getByTestId(`entry-${index}`).getBoundingClientRect = () =>
        box(index * 80 - viewport.scrollLeft, 80);
      screen.getAllByRole("tab")[index]!.getBoundingClientRect = () =>
        box(index * 80 - viewport.scrollLeft, 60);
    });

    rerender(<Entries selected={1} />);
    await act(async () => {});
    expect(viewport.scrollLeft).toBe(10);

    viewport.scrollLeft = 0;
    act(() => screen.getAllByRole("tab")[1]!.focus());
    expect(viewport.scrollLeft).toBe(10);
  });

  /** A held strip of `labels`, laid out, whose row is `scrollWidth` wide. */
  function heldStrip(labels: readonly string[], scrollWidth: number) {
    function Held({ selected, hold }: { selected: number; hold: boolean }) {
      return (
        <TabStripScroller data-testid="held" holdExtent={hold}>
          {labels.map((label, index) => (
            <button key={label} type="button" role="tab" aria-selected={index === selected}>
              {label}
            </button>
          ))}
        </TabStripScroller>
      );
    }
    const { rerender } = render(<Held selected={0} hold={false} />);
    const viewport = screen.getByTestId("held");
    layOut(viewport);
    const content = viewport.firstElementChild as HTMLElement;
    Object.defineProperty(content, "scrollWidth", { configurable: true, value: scrollWidth });
    const show = async (selected: number, hold: boolean) => {
      rerender(<Held selected={selected} hold={hold} />);
      await act(async () => {});
    };
    return { viewport, content, show };
  }

  it("holds its row's width and leaves the scroll alone while held, then reveals the selection", async () => {
    const { viewport, content, show } = heldStrip(["Summary", "Comments", "Changes"], 180);
    viewport.scrollLeft = 80;

    await show(0, true);
    expect(content.style.minWidth).toBe("180px");
    // The selection is out of view, but a held strip doesn't move.
    await show(1, true);
    expect(viewport.scrollLeft).toBe(80);

    await show(0, false);
    expect(content.style.minWidth).toBe("");
    expect(viewport.scrollLeft).toBe(0);
  });

  it("leaves an unscrolled row free to shrink while held, so what follows it moves with it", async () => {
    const { viewport, content, show } = heldStrip(["Summary", "Comments"], 120);
    await show(0, true);
    expect(content.style.minWidth).toBe("");
    expect(viewport.scrollLeft).toBe(0);
  });

  it("reveals the selection when a hold ends on an unscrolled row, though nothing resizes", async () => {
    const { viewport, content, show } = heldStrip(["Summary", "Comments", "Changes"], 180);
    // Held at the start of an overflowing row, a close selects the far-right tab.
    await show(0, true);
    await show(2, true);
    expect(content.style.minWidth).toBe("");
    expect(viewport.scrollLeft).toBe(0);

    await show(2, false);
    expect(viewport.scrollLeft).toBe(80);
  });

  it("passes its props to the viewport and leaves a tab already in view alone", async () => {
    const { rerender } = render(<Strip selected={0} />);
    const viewport = screen.getByTestId("strip");
    layOut(viewport, 150);
    rerender(<Strip selected={1} />);
    await act(async () => {});
    expect(viewport.scrollLeft).toBe(0);
    expect(viewport).toHaveClass("overflow-x-auto");
  });

  it("fades only the edges with tabs past them, following the scroll", () => {
    render(<Strip selected={0} fadeEdges />);
    const viewport = screen.getByTestId("strip");
    Object.defineProperty(viewport, "scrollWidth", { configurable: true, value: 180 });
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 100 });
    const scrollTo = (left: number) =>
      act(() => {
        viewport.scrollLeft = left;
        viewport.dispatchEvent(new Event("scroll"));
      });

    scrollTo(0);
    expect(viewport).not.toHaveAttribute("data-fade-start");
    expect(viewport).toHaveAttribute("data-fade-end");
    scrollTo(40);
    expect(viewport).toHaveAttribute("data-fade-start");
    expect(viewport).toHaveAttribute("data-fade-end");
    scrollTo(80);
    expect(viewport).toHaveAttribute("data-fade-start");
    expect(viewport).not.toHaveAttribute("data-fade-end");
    expect(viewport.style.getPropertyValue("--tab-strip-mask")).toMatch(
      /^linear-gradient\(to right, transparent, .*, #000\)$/,
    );

    Object.defineProperty(viewport, "scrollWidth", { configurable: true, value: 100 });
    scrollTo(0);
    expect(viewport).not.toHaveAttribute("data-fade-end");
    expect(viewport.style.getPropertyValue("--tab-strip-mask")).toBe("");
  });

  it("never fades a strip that didn't ask to", () => {
    render(<Strip selected={0} />);
    const viewport = screen.getByTestId("strip");
    Object.defineProperty(viewport, "scrollWidth", { configurable: true, value: 180 });
    Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 100 });
    act(() => viewport.dispatchEvent(new Event("scroll")));
    expect(viewport).not.toHaveAttribute("data-fade-end");
  });

  describe("the wheel over an overflowing strip", () => {
    function overflowing() {
      render(<Strip selected={0} />);
      const viewport = screen.getByTestId("strip");
      Object.defineProperty(viewport, "scrollWidth", { configurable: true, value: 180 });
      Object.defineProperty(viewport, "clientWidth", { configurable: true, value: 100 });
      return viewport;
    }

    it("scrolls the tabs sideways for a plain vertical wheel", () => {
      const viewport = overflowing();
      expect(fireEvent.wheel(viewport, { deltaY: 30 })).toBe(false);
      expect(viewport.scrollLeft).toBe(30);
    });

    it("leaves Ctrl-wheel and pinches to the page's zoom, and sideways gestures to the browser", () => {
      const viewport = overflowing();
      expect(fireEvent.wheel(viewport, { deltaY: 30, ctrlKey: true })).toBe(true);
      expect(fireEvent.wheel(viewport, { deltaX: 30, ctrlKey: true })).toBe(true);
      expect(fireEvent.wheel(viewport, { deltaX: 30 })).toBe(true);
      expect(viewport.scrollLeft).toBe(0);
    });
  });

  describe("a press on a tab's close button", () => {
    // Two 80px entries in a 150px strip: the second entry (80–160) runs 10px past the edge.
    function pressStrip() {
      render(
        <TabStripScroller data-testid="pressed">
          {[0, 1].map((index) => (
            <div key={index} data-tab-strip-entry data-testid={`entry-${index}`}>
              <button type="button" role="tab" aria-selected={index === 0}>
                Tab {index}
              </button>
              <button type="button" aria-label={`Close Tab ${index}`}>
                <svg data-testid={`close-icon-${index}`} />
              </button>
            </div>
          ))}
        </TabStripScroller>,
      );
      const viewport = screen.getByTestId("pressed");
      const box = (left: number, width: number) =>
        ({
          left,
          right: left + width,
          width,
          top: 0,
          bottom: 20,
          height: 20,
          x: left,
          y: 0,
        }) as DOMRect;
      viewport.getBoundingClientRect = () => box(0, 150);
      [0, 1].forEach((index) => {
        screen.getByTestId(`entry-${index}`).getBoundingClientRect = () =>
          box(index * 80 - viewport.scrollLeft, 80);
      });
      const close = screen.getByRole("button", { name: "Close Tab 1" });
      // The pointer lands on the icon; the press focuses the button around it.
      const press = () => {
        fireEvent.pointerDown(screen.getByTestId("close-icon-1"));
        act(() => close.focus());
      };
      const refocus = () => {
        act(() => close.blur());
        act(() => close.focus());
      };
      return { viewport, close, press, refocus };
    }

    it("leaves the strip where it is, so the release lands on the same button", () => {
      const { viewport, press } = pressStrip();
      press();
      expect(viewport.scrollLeft).toBe(0);
    });

    it.each([
      ["a release outside the strip", () => fireEvent.pointerUp(document.body)],
      ["a cancelled press", () => fireEvent.pointerCancel(document.body)],
      ["leaving the window", () => fireEvent.blur(window)],
    ])("reveals focus again after %s", (_name, end) => {
      const { viewport } = pressStrip();
      fireEvent.pointerDown(screen.getByTestId("close-icon-1"));
      end();
      act(() => screen.getByRole("button", { name: "Close Tab 1" }).focus());
      expect(viewport.scrollLeft).toBe(10);
    });

    it("keeps a tap's press past its release, since a touch focuses the button only then", () => {
      const { viewport, close } = pressStrip();
      // Touch order: pointerdown, pointerup, then the compatibility mousedown focuses.
      fireEvent.pointerDown(screen.getByTestId("close-icon-1"), { pointerType: "touch" });
      fireEvent.pointerUp(screen.getByTestId("close-icon-1"), { pointerType: "touch" });
      act(() => close.focus());
      expect(viewport.scrollLeft).toBe(0);
      fireEvent.click(close);
      act(() => close.blur());
      act(() => close.focus());
      expect(viewport.scrollLeft).toBe(10);
    });

    it("forgets a tap's press once its click lands, though nothing took focus", () => {
      const { viewport, close } = pressStrip();
      fireEvent.pointerDown(screen.getByTestId("close-icon-1"), { pointerType: "pen" });
      fireEvent.pointerUp(screen.getByTestId("close-icon-1"), { pointerType: "pen" });
      fireEvent.click(close);
      act(() => close.focus());
      expect(viewport.scrollLeft).toBe(10);
    });

    it("reveals the next focus after the press's own, which used the press up", () => {
      const { viewport, press, refocus } = pressStrip();
      press();
      refocus();
      expect(viewport.scrollLeft).toBe(10);
    });

    it("reveals keyboard focus while the pointer is still held", () => {
      const { viewport } = pressStrip();
      fireEvent.pointerDown(screen.getByTestId("close-icon-1"));
      // A tab's own key handler moves focus; the window hears the key first.
      const tab = screen.getAllByRole("tab")[0]!;
      tab.addEventListener("keydown", () =>
        screen.getByRole("button", { name: "Close Tab 1" }).focus(),
      );
      act(() => {
        fireEvent.keyDown(tab, { key: "ArrowRight" });
      });
      expect(viewport.scrollLeft).toBe(10);
    });

    it("reveals focus a press sends somewhere other than what it pressed", () => {
      const { viewport } = pressStrip();
      fireEvent.pointerDown(screen.getAllByRole("tab")[0]!);
      act(() => screen.getByRole("button", { name: "Close Tab 1" }).focus());
      expect(viewport.scrollLeft).toBe(10);
    });

    it("stops listening once the strip goes", () => {
      const removed = vi.spyOn(window, "removeEventListener");
      const { unmount } = render(<TabStripScroller>{null}</TabStripScroller>);
      unmount();
      expect(removed.mock.calls.map(([type]) => type)).toEqual(
        expect.arrayContaining(["pointerup", "pointercancel", "click", "keydown", "blur"]),
      );
      removed.mockRestore();
    });
  });
});
