import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSearchParams } from "@/lib/routing";
import { useNavigateToSession } from "@/lib/sessionNavigation";
import { SessionNavigationTestHost } from "@/lib/sessionNavigation.test-utils";
import { canvasSessionHref } from "./canvasNavigation";
import { CanvasStage, useCanvasSplitLayout } from "./CanvasStage";

let width = 1200;
let resize = () => {};
const disconnect = vi.fn();
beforeEach(() => {
  width = 1200;
  disconnect.mockClear();
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    () => new DOMRect(0, 0, width, 900),
  );
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resize = callback;
      }
      observe() {}
      disconnect() {
        disconnect();
      }
    },
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function Harness() {
  const [params] = useSearchParams();
  const id = params.get("session") || undefined;
  const open = useNavigateToSession();
  const layout = useCanvasSplitLayout(true, Boolean(id));
  return (
    <CanvasStage
      enabled
      conversationId={id}
      layout={layout}
      sidebarOpen
      onOpenSidebar={() => {}}
      board={
        <div data-testid="board">
          <button type="button" data-canvas-session-id="a" onClick={() => open("a")}>
            Open A
          </button>
          <input aria-label="Board state" defaultValue="board" />
        </div>
      }
    >
      <input aria-label="Chat draft" defaultValue="draft" />
    </CanvasStage>
  );
}
function renderStage() {
  return render(
    <MemoryRouter initialEntries={["/canvas"]}>
      <SessionNavigationTestHost resolveHref={canvasSessionHref}>
        <Harness />
      </SessionNavigationTestHost>
    </MemoryRouter>,
  );
}

describe("Canvas split layout", () => {
  it("keeps board and one chat instance through wide/narrow layout changes", () => {
    renderStage();
    const board = screen.getByTestId("board");
    fireEvent.click(screen.getByRole("button", { name: "Open A" }));
    const draft = screen.getByRole("textbox", { name: "Chat draft" });
    fireEvent.change(draft, { target: { value: "keep this draft" } });
    expect(screen.getByRole("separator")).toBeInTheDocument();
    act(() => {
      width = 700;
      resize();
    });
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.queryByRole("region", { name: "Canvas board" })).toBeNull();
    expect(screen.getByTestId("board")).toBe(board);
    expect(screen.getByRole("textbox", { name: "Chat draft" })).toBe(draft);
    expect(draft).toHaveValue("keep this draft");
    act(() => {
      width = 1200;
      resize();
    });
    expect(screen.getByRole("textbox", { name: "Chat draft" })).toBe(draft);
    fireEvent.click(screen.getByRole("button", { name: "Close session panel" }));
    expect(screen.queryByRole("textbox", { name: "Chat draft" })).toBeNull();
    expect(screen.getByTestId("board")).toBe(board);
    expect(screen.getByRole("button", { name: "Open A" })).toHaveFocus();
  });

  it("resizes with the keyboard while reserving usable board and chat widths", () => {
    renderStage();
    fireEvent.click(screen.getByRole("button", { name: "Open A" }));
    const divider = screen.getByRole("separator");
    expect(divider).toHaveAttribute("aria-valuenow", "540");
    fireEvent.keyDown(divider, { key: "ArrowRight" });
    expect(divider).toHaveAttribute("aria-valuenow", "572");
    fireEvent.keyDown(divider, { key: "Home" });
    expect(divider).toHaveAttribute("aria-valuenow", "320");
    fireEvent.keyDown(divider, { key: "End" });
    expect(divider).toHaveAttribute("aria-valuenow", "714");
    fireEvent.keyDown(divider, { key: "ArrowRight" });
    expect(divider).toHaveAttribute("aria-valuenow", "714");
  });

  it("disconnects its container observer on unmount", () => {
    const { unmount } = renderStage();
    unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  });
});
