import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspaceRailEdgeHandle, WorkspaceRailGutterHandle } from "./WorkspaceRailResizeHandle";

function handleProps(onPointerDown = vi.fn(), onKeyDown = vi.fn()) {
  return {
    onPointerDown,
    onPointerMove: vi.fn(),
    onPointerUp: vi.fn(),
    onPointerCancel: vi.fn(),
    onLostPointerCapture: vi.fn(),
    onKeyDown,
    role: "separator" as const,
    "aria-orientation": "vertical" as const,
    "aria-label": "Resize panel",
    "aria-disabled": false,
    tabIndex: 0,
  };
}

/** The rail with its separator, and the gutter band beside it, as a floating-panes page has. */
function renderFloatingRail({ pending = false } = {}) {
  const props = handleProps();
  const page = (shown: boolean) => (
    <div>
      <aside aria-label="Workspace">
        <WorkspaceRailEdgeHandle handleProps={props} pending={pending} bare />
      </aside>
      <WorkspaceRailGutterHandle
        handleProps={props}
        pending={pending}
        railWidth={400}
        shown={shown}
      />
    </div>
  );
  const view = render(page(true));
  const gutter = document.querySelector<HTMLElement>("[data-workspace-rail-gutter-handle]")!;
  return { props, gutter, close: () => view.rerender(page(false)) };
}

afterEach(cleanup);

describe("the workspace rail's resize handle beside floating panes", () => {
  it("has one separator, inside the rail, for the keyboard and assistive tech", () => {
    const { gutter } = renderFloatingRail();
    const rail = screen.getByRole("complementary", { name: "Workspace" });
    const [separator] = screen.getAllByRole("separator");
    expect(screen.getAllByRole("separator")).toHaveLength(1);
    expect(rail).toContainElement(separator!);
    expect(separator).toHaveAccessibleName("Resize panel");
    expect(separator).toHaveAttribute("tabindex", "0");
    expect(gutter).toHaveAttribute("aria-hidden", "true");
    expect(gutter).not.toHaveAttribute("tabindex");
  });

  it("drags from the gutter band, which sits over the gap beside the rail", () => {
    const { props, gutter } = renderFloatingRail();
    fireEvent.pointerDown(gutter);
    fireEvent.pointerMove(gutter);
    fireEvent.pointerUp(gutter);
    expect(props.onPointerDown).toHaveBeenCalledTimes(1);
    expect(props.onPointerMove).toHaveBeenCalledTimes(1);
    expect(props.onPointerUp).toHaveBeenCalledTimes(1);
    expect(gutter.style.right).toBe("395px");
    // Wide enough to grab even where the page's styles set no gap.
    expect(gutter.style.width).toBe("calc(var(--pane-gap, 5px) + 5px)");
  });

  it("neither drags nor takes focus while the session is created", () => {
    const { props, gutter } = renderFloatingRail({ pending: true });
    const separator = screen.getByRole("separator", { name: "Resize panel" });
    expect(separator).toHaveAttribute("aria-disabled", "true");
    expect(separator).toHaveAttribute("tabindex", "-1");
    fireEvent.pointerDown(gutter);
    fireEvent.pointerDown(separator);
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(props.onPointerDown).not.toHaveBeenCalled();
    expect(props.onKeyDown).not.toHaveBeenCalled();
  });

  it("stays mounted but hidden while the rail is closed, so the rail never becomes the last child", () => {
    const { props, gutter, close } = renderFloatingRail();
    expect(gutter).toHaveClass("md:min-[48rem]:block");
    close();
    expect(document.querySelector("[data-workspace-rail-gutter-handle]")).toBe(gutter);
    expect(gutter).not.toHaveClass("md:min-[48rem]:block");
    fireEvent.pointerDown(gutter);
    expect(props.onPointerDown).not.toHaveBeenCalled();
  });
});
