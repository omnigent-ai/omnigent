import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as Dialog from "radix-ui/dialog";
import { MemoryRouter, useLocation, useMatch, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CanvasWorkspace, useCanvasWorkspace } from "./CanvasWorkspace";

let boardReady: Promise<void> | null = null;

vi.mock("@/pages/CanvasPage", () => ({
  CanvasPage: ({ selectedSessionId }: { selectedSessionId: string | null }) => {
    if (boardReady) {
      // eslint-disable-next-line no-throw-literal -- Suspense waits for this promise.
      throw boardReady;
    }
    return (
      <div>
        <input aria-label="Board state" defaultValue="Unchanged board" />
        <span data-testid="selected-session">{selectedSessionId}</span>
      </div>
    );
  },
}));

let containerWidth = 1200;
let notifyResize: () => void;

function Conversation() {
  const canvas = useCanvasWorkspace()!;
  return (
    <>
      <button type="button" onClick={canvas.toggleFocus}>
        {canvas.focused ? "Restore canvas" : "Focus session"}
      </button>
      <button type="button" onClick={canvas.closeConversation}>
        {canvas.compact ? "Back to canvas" : "Close session"}
      </button>
      <Dialog.Root>
        <Dialog.Trigger>Open conversation dialog</Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Content aria-describedby={undefined}>
            <Dialog.Title>Conversation options</Dialog.Title>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

function Workspace() {
  const match = useMatch("/canvas/c/:conversationId");
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <button type="button" onClick={() => navigate("/canvas/c/first?canvas=project")}>
        First card
      </button>
      <button type="button" onClick={() => navigate("/canvas/c/second?canvas=project")}>
        Second card
      </button>
      <div data-testid="location">{location.pathname + location.search}</div>
      <CanvasWorkspace active conversationId={match?.params.conversationId}>
        <Conversation />
      </CanvasWorkspace>
    </>
  );
}

async function renderWorkspace(entry = "/canvas?canvas=project") {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Workspace />
    </MemoryRouter>,
  );
  return screen.findByLabelText("Board state");
}

beforeEach(() => {
  localStorage.clear();
  boardReady = null;
  containerWidth = 1200;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
    width: containerWidth,
    height: 800,
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: containerWidth,
    bottom: 800,
    toJSON() {},
  }));
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        notifyResize = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CanvasWorkspace", () => {
  it("keeps sidebar navigation usable while the board is loading", async () => {
    const user = userEvent.setup();
    const onOpenSidebar = vi.fn();
    let finishLoading!: () => void;
    boardReady = new Promise<void>((resolve) => {
      finishLoading = resolve;
    });
    render(
      <TooltipProvider>
        <MemoryRouter>
          <CanvasWorkspace active onOpenSidebar={onOpenSidebar}>
            {null}
          </CanvasWorkspace>
        </MemoryRouter>
      </TooltipProvider>,
    );
    expect(await screen.findByLabelText("Loading Canvas")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open sidebar" }));
    expect(onOpenSidebar).toHaveBeenCalledOnce();
    await act(async () => {
      boardReady = null;
      finishLoading();
    });
    expect(await screen.findByLabelText("Board state")).toBeVisible();
    expect(screen.queryByLabelText("Loading Canvas")).not.toBeInTheDocument();
  });

  it("keeps the board mounted through selection, focus, and closing the session", async () => {
    const board = await renderWorkspace();
    fireEvent.change(board, { target: { value: "Preserved viewport and cards" } });
    fireEvent.click(screen.getByText("First card"));
    expect(screen.getByTestId("selected-session")).toHaveTextContent("first");
    fireEvent.click(screen.getByText("Second card"));
    expect(screen.getByTestId("selected-session")).toHaveTextContent("second");
    fireEvent.click(screen.getByText("Focus session"));
    expect(screen.getByLabelText("Canvas pane")).toHaveClass("hidden");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByLabelText("Canvas pane")).not.toHaveClass("hidden");
    fireEvent.click(screen.getByText("Close session"));
    expect(screen.getByTestId("location")).toHaveTextContent("/canvas?canvas=project");
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.getByLabelText("Board state")).toBe(board);
    expect(board).toHaveValue("Preserved viewport and cards");
  });

  it("persists keyboard resizing within both panes' minimum widths", async () => {
    await renderWorkspace("/canvas/c/first?canvas=project");
    const divider = screen.getByRole("separator");
    const originalWidth = Number(divider.getAttribute("aria-valuenow"));
    fireEvent.keyDown(divider, { key: "ArrowRight" });
    expect(Number(divider.getAttribute("aria-valuenow"))).toBe(originalWidth + 24);
    fireEvent.keyDown(divider, { key: "End" });
    expect(divider).toHaveAttribute("aria-valuenow", "812");
    expect(divider).toHaveAttribute("aria-valuemax", "812");
    expect(localStorage.getItem("omnigent:canvas-split-ratio")).not.toBeNull();
    fireEvent.keyDown(divider, { key: "Home" });
    expect(divider).toHaveAttribute("aria-valuenow", "320");
    expect(divider).toHaveAttribute("aria-valuemin", "320");
  });

  it("lets a nested dialog consume Escape before restoring the canvas", async () => {
    const user = userEvent.setup();
    await renderWorkspace("/canvas/c/first?canvas=project");
    await user.click(screen.getByRole("button", { name: "Focus session" }));
    await user.click(screen.getByRole("button", { name: "Open conversation dialog" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Canvas pane")).toHaveClass("hidden");
    await user.keyboard("{Escape}");
    expect(screen.getByLabelText("Canvas pane")).not.toHaveClass("hidden");
  });

  it("restores and saves the default split on divider double-click", async () => {
    localStorage.setItem("omnigent:canvas-split-ratio", "0.4");
    await renderWorkspace("/canvas/c/first?canvas=project");
    const divider = screen.getByRole("separator");
    expect(divider).toHaveAttribute("aria-valuenow", "477");
    fireEvent.doubleClick(divider);
    expect(divider).toHaveAttribute("aria-valuenow", "644");
    expect(localStorage.getItem("omnigent:canvas-split-ratio")).toBe("0.54");
  });

  it("switches to a single pane on narrow screens and returns to the same board", async () => {
    const board = await renderWorkspace("/canvas/c/first?canvas=project");
    containerWidth = 600;
    act(() => notifyResize());
    expect(screen.getByLabelText("Canvas pane")).toHaveClass("hidden");
    expect(screen.queryByRole("separator")).toBeNull();
    fireEvent.click(screen.getByText("Back to canvas"));
    expect(screen.getByLabelText("Canvas pane")).not.toHaveClass("hidden");
    expect(screen.getByLabelText("Board state")).toBe(board);
    expect(screen.getByTestId("location")).toHaveTextContent("/canvas?canvas=project");
  });

  it("restores a narrow board preference on a wide display", async () => {
    containerWidth = 2800;
    localStorage.setItem("omnigent:canvas-split-ratio", "0.15");
    await renderWorkspace("/canvas/c/first?canvas=project");
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "419");
  });
});
