import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MermaidBlock } from "./MermaidBlock";

function open() {
  render(
    <MermaidBlock
      block={
        <svg id="mermaid-test">
          <rect height="10" width="10" />
        </svg>
      }
    />,
  );
  fireEvent.click(screen.getByLabelText("View fullscreen"));
  return screen.getByTestId("mermaid-fullscreen-content");
}

const scaleOf = (el: HTMLElement) => parseFloat(el.style.transform.split("scale(")[1] ?? "NaN");
const offsetOf = (el: HTMLElement) => {
  const [x, y] = el.style.transform
    .split("translate(")[1]
    .split(")")[0]
    .split(",")
    .map((v) => parseFloat(v));
  return { x, y };
};
const drag = (el: HTMLElement, dx: number, dy: number) => {
  fireEvent.pointerDown(el, { pointerId: 1, button: 0, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(el, { pointerId: 1, clientX: 100 + dx, clientY: 100 + dy });
  fireEvent.pointerUp(el, { pointerId: 1, clientX: 100 + dx, clientY: 100 + dy });
};

describe("MermaidBlock fullscreen zoom", () => {
  it("renders the zoom buttons with labels and titles", () => {
    open();
    ["Zoom in", "Zoom out", "Reset zoom", "Exit fullscreen"].forEach((name) => {
      expect(screen.getByRole("button", { name })).toHaveAttribute("title", name);
    });
  });

  it("zooms in and out by 1.25", () => {
    const content = open();
    expect(scaleOf(content)).toBe(1);
    fireEvent.click(screen.getByLabelText("Zoom in"));
    expect(scaleOf(content)).toBeCloseTo(1.25);
    fireEvent.click(screen.getByLabelText("Zoom out"));
    fireEvent.click(screen.getByLabelText("Zoom out"));
    expect(scaleOf(content)).toBeCloseTo(0.8);
  });

  it("clamps between 0.5 and 4", () => {
    const content = open();
    const zoomIn = screen.getByLabelText("Zoom in");
    const zoomOut = screen.getByLabelText("Zoom out");
    Array.from({ length: 20 }).forEach(() => fireEvent.click(zoomIn));
    expect(scaleOf(content)).toBe(4);
    Array.from({ length: 30 }).forEach(() => fireEvent.click(zoomOut));
    expect(scaleOf(content)).toBe(0.5);
  });

  it("resets to 1 and starts at 1 when reopened", () => {
    const content = open();
    fireEvent.click(screen.getByLabelText("Zoom in"));
    fireEvent.click(screen.getByLabelText("Reset zoom"));
    expect(scaleOf(content)).toBe(1);
    fireEvent.click(screen.getByLabelText("Zoom in"));
    fireEvent.click(screen.getByLabelText("Exit fullscreen"));
    fireEvent.click(screen.getByLabelText("View fullscreen"));
    expect(scaleOf(screen.getByTestId("mermaid-fullscreen-content"))).toBe(1);
  });

  it("zooms on wheel and pinch without scrolling the page", () => {
    const content = open();
    const wheelIn = new WheelEvent("wheel", { deltaY: -100, bubbles: true, cancelable: true });
    act(() => {
      content.dispatchEvent(wheelIn);
    });
    expect(wheelIn.defaultPrevented).toBe(true);
    expect(scaleOf(content)).toBeCloseTo(1.25);
    const pinch = new WheelEvent("wheel", {
      deltaY: 100,
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      content.dispatchEvent(pinch);
    });
    expect(pinch.defaultPrevented).toBe(true);
    expect(scaleOf(content)).toBeCloseTo(1);
  });

  it("keeps the exit button above the zoomed content", () => {
    open();
    expect(screen.getByLabelText("Exit fullscreen").className).toContain("z-10");
  });

  it("ignores a wheel event with no vertical delta", () => {
    const content = open();
    const flat = new WheelEvent("wheel", { deltaY: 0, bubbles: true, cancelable: true });
    act(() => {
      content.dispatchEvent(flat);
    });
    expect(scaleOf(content)).toBe(1);
  });

  it("zooms by less than one step for a small wheel delta", () => {
    const content = open();
    const small = new WheelEvent("wheel", {
      deltaY: -4,
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    act(() => {
      content.dispatchEvent(small);
    });
    expect(scaleOf(content)).toBeGreaterThan(1);
    expect(scaleOf(content)).toBeLessThan(1.25);
  });

  it("closes on a click on empty space but not on the diagram", () => {
    const content = open();
    fireEvent.click(content.querySelector("rect")!);
    expect(screen.getByTestId("mermaid-fullscreen-content")).toBeInTheDocument();
    fireEvent.pointerDown(content, { pointerId: 1, button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerUp(content, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.click(content);
    expect(screen.queryByTestId("mermaid-fullscreen-content")).toBeNull();
  });

  it("pans on drag at scale 1 and when zoomed in", () => {
    const content = open();
    expect(content.style.cursor).toBe("grab");
    drag(content, 30, -20);
    expect(offsetOf(content)).toEqual({ x: 30, y: -20 });
    fireEvent.click(screen.getByLabelText("Zoom in"));
    drag(content, 10, 10);
    expect(offsetOf(content)).toEqual({ x: 40, y: -10 });
    expect(scaleOf(content)).toBeCloseTo(1.25);
  });

  it("pans below scale 1", () => {
    const content = open();
    fireEvent.click(screen.getByLabelText("Zoom out"));
    expect(scaleOf(content)).toBeLessThan(1);
    drag(content, 15, 25);
    expect(offsetOf(content)).toEqual({ x: 15, y: 25 });
  });

  it("captures the pointer while dragging and stops on lost capture", () => {
    const content = open();
    const setCapture = vi.fn();
    content.setPointerCapture = setCapture;
    fireEvent.pointerDown(content, { pointerId: 7, button: 0, clientX: 0, clientY: 0 });
    expect(setCapture).toHaveBeenCalledWith(7);
    fireEvent.lostPointerCapture(content, { pointerId: 7 });
    expect(content.style.cursor).toBe("grab");
    fireEvent.pointerMove(content, { pointerId: 7, clientX: 50, clientY: 50 });
    expect(offsetOf(content)).toEqual({ x: 0, y: 0 });
  });

  it("ignores a second pointer during a drag and skips release without capture", () => {
    const content = open();
    const release = vi.fn();
    content.releasePointerCapture = release;
    content.hasPointerCapture = () => false;
    fireEvent.pointerDown(content, { pointerId: 1, button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerDown(content, { pointerId: 2, button: 0, clientX: 500, clientY: 500 });
    fireEvent.pointerMove(content, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(offsetOf(content)).toEqual({ x: 10, y: 10 });
    fireEvent.lostPointerCapture(content, { pointerId: 1 });
    expect(release).not.toHaveBeenCalled();
    expect(content.style.cursor).toBe("grab");
  });

  it("shows grabbing while dragging", () => {
    const content = open();
    fireEvent.pointerDown(content, { pointerId: 1, button: 0, clientX: 0, clientY: 0 });
    expect(content.style.cursor).toBe("grabbing");
    fireEvent.pointerUp(content, { pointerId: 1 });
    expect(content.style.cursor).toBe("grab");
  });

  it("clears the offset on reset and on reopen", () => {
    const content = open();
    drag(content, 30, 30);
    fireEvent.click(screen.getByLabelText("Reset zoom"));
    expect(offsetOf(content)).toEqual({ x: 0, y: 0 });
    drag(content, 5, 5);
    fireEvent.click(screen.getByLabelText("Exit fullscreen"));
    fireEvent.click(screen.getByLabelText("View fullscreen"));
    expect(offsetOf(screen.getByTestId("mermaid-fullscreen-content"))).toEqual({ x: 0, y: 0 });
  });

  it("does not pan from a toolbar button drag", () => {
    const content = open();
    drag(screen.getByLabelText("Zoom in"), 30, 30);
    expect(offsetOf(content)).toEqual({ x: 0, y: 0 });
  });

  it("stays open when a drag ends on empty space", () => {
    const content = open();
    drag(content, 50, 50);
    fireEvent.click(content);
    expect(screen.getByTestId("mermaid-fullscreen-content")).toBeInTheDocument();
  });
});
