import { fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SessionDockSplitter } from "./SessionDockSplitter";

function SplitterHarness({ direction }: { direction: "column" | "row" }) {
  const dockRef = useRef<HTMLDivElement>(null);
  const [sizePct, setSizePct] = useState(45);
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div
      ref={dockRef}
      data-testid="dock"
      style={{ display: "flex", flexDirection: direction, width: 1000, height: 400 }}
    >
      <SessionDockSplitter
        dockRef={dockRef}
        label="First task"
        sizePct={sizePct}
        onResize={setSizePct}
        onCollapse={() => setCollapsed(true)}
      />
      <output data-testid="size">{sizePct}</output>
      <output data-testid="collapsed">{String(collapsed)}</output>
    </div>
  );
}

function setDockRect() {
  vi.spyOn(screen.getByTestId("dock"), "getBoundingClientRect").mockReturnValue({
    left: 0,
    right: 1000,
    top: 0,
    bottom: 400,
    width: 1000,
    height: 400,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
}

describe("SessionDockSplitter", () => {
  it("resizes a bottom-docked workspace vertically", () => {
    render(<SplitterHarness direction="column" />);
    setDockRect();
    const separator = screen.getByRole("separator", { name: "Resize workspace for First task" });

    fireEvent.pointerDown(separator, { pointerId: 1 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientY: 300 });

    expect(screen.getByTestId("dock").style.getPropertyValue("--session-workspace-basis")).toBe(
      "25%",
    );
    fireEvent.pointerUp(separator, { pointerId: 1, clientY: 300 });
    expect(screen.getByTestId("size")).toHaveTextContent("25");
  });

  it("resizes a right-docked workspace horizontally", () => {
    render(<SplitterHarness direction="row" />);
    setDockRect();
    const separator = screen.getByRole("separator", { name: "Resize workspace for First task" });

    fireEvent.pointerDown(separator, { pointerId: 2 });
    fireEvent.pointerMove(separator, { pointerId: 2, clientX: 600 });
    fireEvent.pointerUp(separator, { pointerId: 2, clientX: 600 });

    expect(screen.getByTestId("size")).toHaveTextContent("40");
  });

  it("collapses when dragged below the SP2K minimum", () => {
    render(<SplitterHarness direction="column" />);
    setDockRect();
    const separator = screen.getByRole("separator", { name: "Resize workspace for First task" });

    fireEvent.pointerDown(separator, { pointerId: 3 });
    fireEvent.pointerMove(separator, { pointerId: 3, clientY: 380 });
    fireEvent.pointerUp(separator, { pointerId: 3, clientY: 380 });

    expect(screen.getByTestId("collapsed")).toHaveTextContent("true");
  });
});
