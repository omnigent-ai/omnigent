import { Profiler } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setEmbedRoot } from "@/lib/host";
import { useCursorTooltip } from "./useCursorTooltip";

// jsdom has no layout: a 1000x800 viewport and a 200x30 tooltip box.
beforeEach(() => {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1000);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width: 200,
    height: 30,
  } as DOMRect);
});

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// Mirrors a virtualized FolderTree row: the wrapper's transform would become
// the containing block for a fixed tooltip rendered inside it.
function TransformedRow({ text = "folder/file.ts" }: { text?: string }) {
  const { handlers, tooltip } = useCursorTooltip(text);

  return (
    <div data-testid="row" style={{ transform: "translateY(600px)" }}>
      <span {...handlers}>file.ts</span>
      {tooltip}
    </div>
  );
}

function hoverAt(clientX: number, clientY: number) {
  render(<TransformedRow />);
  fireEvent.mouseMove(screen.getByText("file.ts"), { clientX, clientY });
  return screen.getByText("folder/file.ts");
}

describe("useCursorTooltip", () => {
  it("portals the hovered tooltip to the body and removes it on mouse leave", () => {
    const tooltip = hoverAt(120, 200);

    expect(tooltip.parentElement).toBe(document.body);
    expect(screen.getByTestId("row")).not.toContainElement(tooltip);
    expect(tooltip).toHaveStyle({
      position: "fixed",
      left: "0px",
      top: "0px",
      transform: "translate(120px, 214px)",
    });

    fireEvent.mouseLeave(screen.getByText("file.ts"));
    expect(screen.queryByText("folder/file.ts")).not.toBeInTheDocument();
  });

  it("portals into the embed root when one is registered", () => {
    const embedRoot = document.createElement("div");
    document.body.appendChild(embedRoot);
    setEmbedRoot(embedRoot);

    expect(hoverAt(120, 200).parentElement).toBe(embedRoot);
  });

  // Margin 8, 14px below the pointer, 8px above it.
  it.each([
    { name: "above the pointer near the bottom edge", pointer: [120, 780], left: 120, top: 742 },
    { name: "left of the pointer near the right edge", pointer: [900, 200], left: 700, top: 214 },
  ])("flips $name", ({ pointer, left, top }) => {
    expect(hoverAt(pointer[0], pointer[1])).toHaveStyle({
      transform: `translate(${left}px, ${top}px)`,
    });
  });

  it("coalesces moves into one frame without rerendering or remeasuring the row", () => {
    const frames: FrameRequestCallback[] = [];
    const requestFrame = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        frames.push(callback);
        return frames.length;
      });
    const onRender = vi.fn();
    render(
      <Profiler id="row" onRender={onRender}>
        <TransformedRow />
      </Profiler>,
    );
    const label = screen.getByText("file.ts");
    fireEvent.mouseMove(label, { clientX: 120, clientY: 200 });
    const commits = onRender.mock.calls.length;
    const measurements = vi.mocked(HTMLElement.prototype.getBoundingClientRect).mock.calls.length;

    fireEvent.mouseMove(label, { clientX: 400, clientY: 300 });
    fireEvent.mouseMove(label, { clientX: 900, clientY: 780 });
    expect(onRender).toHaveBeenCalledTimes(commits);
    expect(requestFrame).toHaveBeenCalledTimes(1);
    act(() => frames[0](0));

    expect(screen.getByText("folder/file.ts")).toHaveStyle({
      transform: "translate(700px, 742px)",
    });
    fireEvent.mouseMove(label, { clientX: 120, clientY: 200 });
    expect(requestFrame).toHaveBeenCalledTimes(2);
    act(() => frames[1](16));
    expect(screen.getByText("folder/file.ts")).toHaveStyle({
      transform: "translate(120px, 214px)",
    });
    expect(onRender).toHaveBeenCalledTimes(commits);
    expect(HTMLElement.prototype.getBoundingClientRect).toHaveBeenCalledTimes(measurements);
  });

  it("remeasures text, font size, and viewport changes while the pointer stays still", () => {
    let resizeTooltip = () => {};
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeTooltip = () => callback([], this);
        }
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    const { rerender } = render(<TransformedRow />);
    fireEvent.mouseMove(screen.getByText("file.ts"), { clientX: 900, clientY: 780 });
    const tooltip = screen.getByText("folder/file.ts");
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({
      width: 400,
      height: 60,
    } as DOMRect);
    rerender(<TransformedRow text="longer/folder/file.ts" />);
    expect(tooltip).toHaveStyle({ transform: "translate(500px, 712px)" });

    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({
      width: 500,
      height: 90,
    } as DOMRect);
    act(resizeTooltip);
    expect(tooltip).toHaveStyle({ transform: "translate(400px, 682px)" });

    vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1200);
    vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(1000);
    fireEvent(window, new Event("resize"));
    expect(tooltip).toHaveStyle({ maxWidth: "1184px", transform: "translate(400px, 794px)" });
  });

  it.each(["leave", "unmount"])("cancels pending movement and observers on %s", (action) => {
    vi.spyOn(window, "requestAnimationFrame").mockReturnValue(123);
    const cancelFrame = vi.spyOn(window, "cancelAnimationFrame");
    const disconnect = vi.spyOn(ResizeObserver.prototype, "disconnect");
    const { unmount } = render(<TransformedRow />);
    const label = screen.getByText("file.ts");
    fireEvent.mouseMove(label, { clientX: 120, clientY: 200 });
    fireEvent.mouseMove(label, { clientX: 400, clientY: 300 });

    if (action === "leave") fireEvent.mouseLeave(label);
    else unmount();

    expect(cancelFrame).toHaveBeenCalledWith(123);
    expect(disconnect).toHaveBeenCalledOnce();
    expect(screen.queryByText("folder/file.ts")).not.toBeInTheDocument();
    const measurements = vi.mocked(HTMLElement.prototype.getBoundingClientRect).mock.calls.length;
    fireEvent(window, new Event("resize"));
    expect(HTMLElement.prototype.getBoundingClientRect).toHaveBeenCalledTimes(measurements);
  });
});
