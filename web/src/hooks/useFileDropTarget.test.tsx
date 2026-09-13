import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFileDropTarget } from "./useFileDropTarget";

/** A dataTransfer for an OS file drag. */
function fileDrag(files: File[] = []) {
  return { types: ["Files"], files };
}

/** A drop target with a child inside it and a sibling outside it. */
function Harness({ onFiles }: { onFiles: (files: File[]) => void }) {
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const active = useFileDropTarget(target, onFiles);
  return (
    <div>
      <div ref={setTarget} data-testid="target">
        <div data-testid="inside">transcript</div>
        <div data-testid="state">{active ? "active" : "idle"}</div>
      </div>
      <div data-testid="outside">sidebar</div>
    </div>
  );
}

function state(): string {
  return screen.getByTestId("state").textContent ?? "";
}

afterEach(cleanup);

describe("useFileDropTarget", () => {
  it("attaches files dropped anywhere inside the target", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const file = new File(["x"], "shot.png", { type: "image/png" });

    fireEvent.drop(screen.getByTestId("inside"), { dataTransfer: fileDrag([file]) });

    expect(onFiles).toHaveBeenCalledWith([file]);
  });

  // The shell around the chat is not an attachment surface.
  it("ignores a drop outside the target", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const file = new File(["x"], "shot.png", { type: "image/png" });

    fireEvent.dragEnter(screen.getByTestId("outside"), { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
    const dropped = fireEvent.drop(screen.getByTestId("outside"), {
      dataTransfer: fileDrag([file]),
    });

    // Not default-prevented: nothing claimed the drop.
    expect(dropped).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("reports the drag while it is over the target and clears it on drop", () => {
    render(<Harness onFiles={vi.fn()} />);
    expect(state()).toBe("idle");
    fireEvent.dragEnter(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    expect(state()).toBe("active");
    fireEvent.drop(screen.getByTestId("target"), {
      dataTransfer: fileDrag([new File(["x"], "a.txt")]),
    });
    expect(state()).toBe("idle");
  });

  // Without the depth count the cue flickers off on every child crossing.
  it("keeps the drag active while the pointer crosses child elements", () => {
    render(<Harness onFiles={vi.fn()} />);
    const target = screen.getByTestId("target");
    const inside = screen.getByTestId("inside");

    fireEvent.dragEnter(target, { dataTransfer: fileDrag() });
    fireEvent.dragEnter(inside, { dataTransfer: fileDrag() });
    fireEvent.dragLeave(target, { dataTransfer: fileDrag() });
    expect(state()).toBe("active");

    // Leaving the last entered element ends the drag.
    fireEvent.dragLeave(inside, { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  it("clears the drag when it is cancelled", () => {
    render(<Harness onFiles={vi.fn()} />);
    fireEvent.dragEnter(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    fireEvent.dragEnd(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  // Dragging selected text into the composer textarea has to keep working.
  it("ignores a drag that carries no files", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const transfer = { types: ["text/plain"], files: [] };

    fireEvent.dragEnter(screen.getByTestId("inside"), { dataTransfer: transfer });
    expect(state()).toBe("idle");
    const dropped = fireEvent.drop(screen.getByTestId("inside"), { dataTransfer: transfer });
    // The browser still handles the text drop itself.
    expect(dropped).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });

  // No preventDefault on dragover → no drop event, and the browser opens the
  // file over the app.
  it("claims a file drag so the browser does not open the file", () => {
    render(<Harness onFiles={vi.fn()} />);
    const target = screen.getByTestId("target");
    expect(fireEvent.dragOver(target, { dataTransfer: fileDrag() })).toBe(false);
    expect(state()).toBe("active");
  });

  it("stops listening once unmounted", () => {
    const onFiles = vi.fn();
    const view = render(<Harness onFiles={onFiles} />);
    const target = screen.getByTestId("target");
    view.unmount();

    fireEvent.drop(target, { dataTransfer: fileDrag([new File(["x"], "a.txt")]) });

    expect(onFiles).not.toHaveBeenCalled();
  });
});
