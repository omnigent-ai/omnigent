import { cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useResizeDrag } from "./useResizeDrag";

const overlaySelector = () =>
  [...document.body.children].find(
    (child): child is HTMLElement =>
      child instanceof HTMLElement &&
      child.style.position === "fixed" &&
      child.style.zIndex === "2147483647",
  ) ?? null;

function DragHandle({ name, onMove }: { name: string; onMove: () => void }) {
  const { cancelDrag, handleProps } = useResizeDrag<HTMLDivElement>({
    onMove,
    overlay: true,
  });
  return (
    <>
      <div aria-label={name} {...handleProps} />
      <button type="button" onClick={cancelDrag}>
        Cancel {name}
      </button>
    </>
  );
}

function installPointerCapture(element: HTMLElement) {
  const captured = new Set<number>();
  const setPointerCapture = vi.fn((pointerId: number) => captured.add(pointerId));
  const releasePointerCapture = vi.fn((pointerId: number) => captured.delete(pointerId));
  Object.assign(element, { setPointerCapture, releasePointerCapture });
  return { releasePointerCapture, setPointerCapture };
}

function startDrag(element: HTMLElement, pointerId: number): void {
  fireEvent.pointerDown(element, { button: 0, pointerId });
}

afterEach(() => {
  cleanup();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});

describe("useResizeDrag cancellation", () => {
  it.each([
    ["Escape", () => fireEvent.keyDown(document, { key: "Escape" })],
    ["context menu", () => document.dispatchEvent(new Event("contextmenu"))],
    ["window blur", () => window.dispatchEvent(new Event("blur"))],
    ["tab visibility change", () => document.dispatchEvent(new Event("visibilitychange"))],
    ["programmatic cancellation", () => fireEvent.click(screen.getByRole("button"))],
  ])("fully releases a real DOM drag on %s", (_signal, abort) => {
    const onMove = vi.fn();
    render(<DragHandle name="Resize first" onMove={onMove} />);
    const handle = screen.getByLabelText("Resize first");
    const capture = installPointerCapture(handle);
    document.body.style.cursor = "crosshair";
    document.body.style.userSelect = "text";

    startDrag(handle, 17);
    expect(capture.setPointerCapture).toHaveBeenCalledWith(17);
    expect(overlaySelector()).not.toBeNull();
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");

    abort();

    expect(capture.releasePointerCapture).toHaveBeenCalledWith(17);
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("crosshair");
    expect(document.body.style.userSelect).toBe("text");
    fireEvent.pointerMove(handle, { pointerId: 17 });
    expect(onMove).not.toHaveBeenCalled();
  });

  it("keeps shared body styles until every concurrent hook finishes", () => {
    render(
      <>
        <DragHandle name="Resize first" onMove={vi.fn()} />
        <DragHandle name="Resize second" onMove={vi.fn()} />
      </>,
    );
    const first = screen.getByLabelText("Resize first");
    const second = screen.getByLabelText("Resize second");
    installPointerCapture(first);
    installPointerCapture(second);
    document.body.style.cursor = "crosshair";
    document.body.style.userSelect = "text";

    startDrag(first, 1);
    startDrag(second, 2);
    fireEvent.pointerUp(first, { pointerId: 1 });

    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");

    fireEvent.pointerUp(second, { pointerId: 2 });
    expect(document.body.style.cursor).toBe("crosshair");
    expect(document.body.style.userSelect).toBe("text");
  });
});

function LifecycleHandle({
  onStart,
  onCommit,
  onCancel,
}: {
  onStart: () => void;
  onCommit: () => void;
  onCancel: () => void;
}) {
  const { handleProps } = useResizeDrag<HTMLDivElement>({
    onStart,
    onMove: () => {},
    onCommit,
    onCancel,
  });
  return <div aria-label="Lifecycle handle" {...handleProps} />;
}

describe("useResizeDrag lifecycle callbacks", () => {
  it("preserves handler identity across unchanged rerenders", () => {
    const { result, rerender } = renderHook(() => useResizeDrag({ onMove: vi.fn() }));
    const handlers = result.current.handleProps;

    rerender();
    expect(result.current.handleProps).toBe(handlers);
    rerender();
    expect(result.current.handleProps).toBe(handlers);
  });

  it("fires onStart at pointer down and onCommit (not onCancel) on release", () => {
    const onStart = vi.fn();
    const onCommit = vi.fn();
    const onCancel = vi.fn();
    render(<LifecycleHandle onStart={onStart} onCommit={onCommit} onCancel={onCancel} />);
    const handle = screen.getByLabelText("Lifecycle handle");
    installPointerCapture(handle);

    startDrag(handle, 21);
    expect(onStart).toHaveBeenCalledTimes(1);

    fireEvent.pointerUp(handle, { pointerId: 21 });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("fires onCancel (not onCommit) when the drag aborts on Escape", () => {
    const onStart = vi.fn();
    const onCommit = vi.fn();
    const onCancel = vi.fn();
    render(<LifecycleHandle onStart={onStart} onCommit={onCommit} onCancel={onCancel} />);
    const handle = screen.getByLabelText("Lifecycle handle");
    installPointerCapture(handle);

    startDrag(handle, 22);
    fireEvent.keyDown(document, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCommit).not.toHaveBeenCalled();

    // A dead drag does not re-fire callbacks on a stray release.
    fireEvent.pointerUp(handle, { pointerId: 22 });
    expect(onCommit).not.toHaveBeenCalled();
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
