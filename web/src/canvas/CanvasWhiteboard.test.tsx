// Tests for the Canvas whiteboard. React Flow is stubbed at its seams: the
// viewport portal renders in place, screen and flow coordinates are the same,
// and the session cards come from `nodesRef`. Scene and storage run for real.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { CanvasWhiteboard } from "./CanvasWhiteboard";
import { MAX_ELEMENTS } from "./whiteboardScene";
import { readWhiteboard, whiteboardStorageKey, writeWhiteboard } from "./whiteboardStorage";

const { nodesRef, zoomRef, snapRequests } = vi.hoisted(() => ({
  nodesRef: {
    current: [] as { id: string; position: { x: number; y: number }; measured?: object }[],
  },
  zoomRef: { current: 1 },
  snapRequests: [] as boolean[],
}));

vi.mock("@xyflow/react", () => ({
  ViewportPortal: ({ children }: { children: ReactNode }) => children,
  useNodes: () => nodesRef.current,
  useReactFlow: () => ({
    screenToFlowPosition: (point: { x: number; y: number }, options?: { snapToGrid?: boolean }) => {
      snapRequests.push(options?.snapToGrid ?? true);
      return point;
    },
    getZoom: () => zoomRef.current,
  }),
}));

function card(id: string, x: number, y: number) {
  return { id, position: { x, y }, measured: { width: 100, height: 50 } };
}

function surface() {
  return screen.getByTestId("whiteboard-surface");
}

function drag(target: Element, from: [number, number], to: [number, number], pointerId = 1) {
  fireEvent.pointerDown(target, { pointerId, button: 0, clientX: from[0], clientY: from[1] });
  fireEvent.pointerMove(target, { pointerId, clientX: to[0], clientY: to[1] });
  fireEvent.pointerUp(target, { pointerId, clientX: to[0], clientY: to[1] });
}

function chooseTool(label: string) {
  fireEvent.click(screen.getByRole("button", { name: label }));
}

beforeEach(() => {
  nodesRef.current = [];
  zoomRef.current = 1;
  snapRequests.length = 0;
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("CanvasWhiteboard", () => {
  it("draws a frame by dragging, saves it, and hands the board back to Select", () => {
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(screen.queryByTestId("whiteboard-surface")).toBeNull();

    chooseTool("Frame");
    expect(screen.getByRole("button", { name: "Frame" })).toHaveAttribute("aria-pressed", "true");
    drag(surface(), [10, 20], [210, 120]);

    expect(screen.getAllByTestId("whiteboard-frame")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Select" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("whiteboard-surface")).toBeNull();
    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({ kind: "rectangle", x: 10, y: 20, width: 200, height: 100 }),
    ]);
    // React Flow snaps by default; frames ask for it explicitly.
    expect(snapRequests.length).toBeGreaterThan(0);
    expect(snapRequests.every(Boolean)).toBe(true);
  });

  it("keeps the pen active and saves one stroke per gesture", () => {
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Draw");
    drag(surface(), [0, 0], [40, 40]);
    drag(surface(), [100, 0], [140, 10], 2);

    expect(screen.getAllByTestId("whiteboard-stroke")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Draw" })).toHaveAttribute("aria-pressed", "true");
    expect(readWhiteboard("u", "main").map((element) => element.kind)).toEqual([
      "stroke",
      "stroke",
    ]);
    // Snapping pen samples to the grid turns strokes into staircases.
    expect(snapRequests.length).toBeGreaterThan(0);
    expect(snapRequests.some(Boolean)).toBe(false);
  });

  it("places a note, edits its text, and shows it again after a reload", () => {
    const { unmount } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Note");
    fireEvent.pointerDown(surface(), { pointerId: 1, button: 0, clientX: 200, clientY: 200 });
    fireEvent.pointerUp(surface(), { pointerId: 1, clientX: 200, clientY: 200 });

    const editor = screen.getByRole("textbox", { name: "Note text" });
    expect(editor).toHaveFocus();
    fireEvent.change(editor, { target: { value: "Ask the reviewer" } });
    fireEvent.blur(editor);
    unmount();

    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(screen.getByRole("note", { name: "Ask the reviewer" })).toBeInTheDocument();
  });

  it("keeps note text typed just before a reload, without leaving the editor", () => {
    const { unmount } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Note");
    fireEvent.pointerDown(surface(), { pointerId: 1, button: 0, clientX: 200, clientY: 200 });
    fireEvent.pointerUp(surface(), { pointerId: 1, clientX: 200, clientY: 200 });
    fireEvent.change(screen.getByRole("textbox", { name: "Note text" }), {
      target: { value: "Half-written" },
    });

    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({ kind: "note", text: "Half-written" }),
    ]);
    unmount();
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(screen.getByRole("note", { name: "Half-written" })).toBeInTheDocument();
  });

  it("saves a frame name typed before the board unmounts", () => {
    writeWhiteboard(
      [{ id: "wb-frame", kind: "rectangle", x: 0, y: 0, width: 320, height: 192, text: "" }],
      "u",
      "main",
    );
    const { unmount } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    fireEvent.doubleClick(within(screen.getByTestId("whiteboard-frame")).getByLabelText("Frame"));
    fireEvent.change(screen.getByRole("textbox", { name: "Frame name" }), {
      target: { value: "Review" },
    });
    unmount();
    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({ kind: "rectangle", text: "Review" }),
    ]);
  });

  it("binds an arrow to the session cards under its ends, so it follows them", () => {
    nodesRef.current = [card("session-a", 0, 0), card("session-b", 400, 0)];
    const { rerender } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Arrow");
    drag(surface(), [50, 25], [450, 25]);

    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({
        kind: "arrow",
        from: { bind: "session-a" },
        to: { bind: "session-b" },
      }),
    ]);
    const line = () => screen.getByTestId("whiteboard-arrow").querySelector("line");
    expect(line()).toHaveAttribute("x2", "400");

    nodesRef.current = [card("session-a", 0, 0), card("session-b", 400, 300)];
    rerender(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(line()).toHaveAttribute("y2", "300");
  });

  it("drops an arrow that starts and ends on the same card", () => {
    nodesRef.current = [card("session-a", 0, 0)];
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Arrow");
    drag(surface(), [10, 10], [90, 40]);
    expect(screen.queryByTestId("whiteboard-arrow")).toBeNull();
    expect(readWhiteboard("u", "main")).toEqual([]);
  });

  it("moves a note by the drag distance at the current zoom, then deletes it", () => {
    writeWhiteboard(
      [{ id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "Plan" }],
      "u",
      "main",
    );
    zoomRef.current = 2;
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    drag(screen.getByRole("note", { name: "Plan" }), [0, 0], [130, 60]);
    // 130×60 screen pixels at 2× zoom is 65×30 on the board, snapped to the 32px grid.
    expect(readWhiteboard("u", "main")).toEqual([expect.objectContaining({ x: 64, y: 32 })]);

    expect(screen.getByRole("button", { name: "Delete selected drawing" })).toBeEnabled();
    act(() => {
      fireEvent.keyDown(window, { key: "Delete" });
    });
    expect(screen.queryByRole("note")).toBeNull();
    expect(readWhiteboard("u", "main")).toEqual([]);
  });

  it("leaves Delete to a text field that has focus", () => {
    writeWhiteboard(
      [{ id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "Plan" }],
      "u",
      "main",
    );
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    const note = screen.getByRole("note", { name: "Plan" });
    fireEvent.pointerDown(note, { pointerId: 1, button: 0, clientX: 0, clientY: 0 });
    fireEvent.pointerUp(note, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.doubleClick(note);

    fireEvent.keyDown(screen.getByRole("textbox", { name: "Note text" }), { key: "Backspace" });
    expect(screen.getByRole("textbox", { name: "Note text" })).toBeInTheDocument();
    expect(readWhiteboard("u", "main")).toHaveLength(1);
  });

  it("returns to Select on Escape without drawing", () => {
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Draw");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("whiteboard-surface")).toBeNull();
    expect(screen.getByRole("button", { name: "Select" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps each canvas's drawing apart", () => {
    writeWhiteboard(
      [{ id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "Main only" }],
      "u",
      "main",
    );
    const { rerender } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(screen.getByRole("note", { name: "Main only" })).toBeInTheDocument();

    rerender(<CanvasWhiteboard canvasId="project-1" viewerId="u" />);
    expect(screen.queryByRole("note")).toBeNull();

    rerender(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    expect(screen.getByRole("note", { name: "Main only" })).toBeInTheDocument();
  });

  it("writes a note edit to its own canvas when the canvas changes before it is saved", () => {
    writeWhiteboard(
      [{ id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "" }],
      "u",
      "main",
    );
    const { rerender } = render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    fireEvent.doubleClick(screen.getByRole("note", { name: "Empty note" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Note text" }), {
      target: { value: "Main draft" },
    });

    // No blur: the canvas changes while the edit is still waiting to be saved.
    rerender(<CanvasWhiteboard canvasId="project-1" viewerId="u" />);
    chooseTool("Frame");
    drag(surface(), [0, 0], [100, 100]);

    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({ id: "wb-note", text: "Main draft" }),
    ]);
    expect(readWhiteboard("u", "project-1")).toEqual([
      expect.objectContaining({ kind: "rectangle" }),
    ]);
  });

  it("keeps both tabs' drawings when two tabs edit the same board", () => {
    const tabA = within(render(<CanvasWhiteboard canvasId="main" viewerId="u" />).container);
    const tabB = within(render(<CanvasWhiteboard canvasId="main" viewerId="u" />).container);

    fireEvent.click(tabA.getByRole("button", { name: "Frame" }));
    drag(tabA.getByTestId("whiteboard-surface"), [0, 0], [100, 100]);
    // Tab B still shows the empty board it loaded.
    expect(tabB.queryByTestId("whiteboard-frame")).toBeNull();
    fireEvent.click(tabB.getByRole("button", { name: "Draw" }));
    drag(tabB.getByTestId("whiteboard-surface"), [200, 0], [240, 40]);

    expect(readWhiteboard("u", "main").map((element) => element.kind)).toEqual([
      "rectangle",
      "stroke",
    ]);
    expect(tabB.getAllByTestId("whiteboard-frame")).toHaveLength(1);
  });

  it("shows another tab's saved drawing without losing an unsaved note edit", () => {
    writeWhiteboard(
      [{ id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "" }],
      "u",
      "main",
    );
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    fireEvent.doubleClick(screen.getByRole("note", { name: "Empty note" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Note text" }), {
      target: { value: "Typed here" },
    });

    // The other tab adds a frame; the browser tells this tab with a storage event.
    writeWhiteboard(
      [
        { id: "wb-note", kind: "note", x: 0, y: 0, width: 192, height: 128, text: "" },
        { id: "wb-frame", kind: "rectangle", x: 300, y: 0, width: 320, height: 192, text: "" },
      ],
      "u",
      "main",
    );
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: whiteboardStorageKey("u", "main"),
          storageArea: window.localStorage,
        }),
      );
    });
    expect(screen.getAllByTestId("whiteboard-frame")).toHaveLength(1);

    fireEvent.blur(screen.getByRole("textbox", { name: "Note text" }));
    expect(readWhiteboard("u", "main")).toEqual([
      expect.objectContaining({ id: "wb-note", text: "Typed here" }),
      expect.objectContaining({ id: "wb-frame" }),
    ]);
  });

  it("refuses a new drawing on a full board and says why, keeping every saved one", () => {
    const full = Array.from({ length: MAX_ELEMENTS }, (_, index) => ({
      id: `wb-${index}`,
      kind: "stroke" as const,
      points: [
        { x: index, y: 0 },
        { x: index, y: 1 },
      ],
    }));
    writeWhiteboard(full, "u", "main");
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Frame");
    drag(surface(), [0, 0], [100, 100]);

    expect(screen.queryByTestId("whiteboard-frame")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(
      "This board is full. Delete a drawing to add another.",
    );
    const stored = readWhiteboard("u", "main");
    expect(stored).toHaveLength(MAX_ELEMENTS);
    expect(stored[0].id).toBe("wb-0");
  });

  it("says so when the drawing cannot be saved", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    render(<CanvasWhiteboard canvasId="main" viewerId="u" />);
    chooseTool("Frame");
    drag(surface(), [0, 0], [100, 100]);

    expect(screen.getByRole("status")).toHaveTextContent("Drawing could not be saved.");
    expect(screen.getAllByTestId("whiteboard-frame")).toHaveLength(1);
  });
});
