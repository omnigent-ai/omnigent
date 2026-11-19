import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef, type MutableRefObject } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as OutboxModule from "./core/designOutbox";
import type * as CaptureModule from "./desktopCapture";

const queued = vi.hoisted(() => vi.fn((_request: unknown) => "request_1"));
vi.mock("./core/designOutbox", async (importOriginal) => ({
  ...(await importOriginal<typeof OutboxModule>()),
  queueDesignRequest: queued,
}));
// jsdom has no canvas: Draw's screenshot is the capture, with what was drawn on it.
const drawn = vi.hoisted(() => vi.fn());
vi.mock("./desktopCapture", async (importOriginal) => ({
  ...(await importOriginal<typeof CaptureModule>()),
  drawingScreenshot: async (shot: { viewport: unknown }, shapes: unknown) => {
    drawn(shapes, shot.viewport);
    return new File(["png"], "design-drawing.png", { type: "image/png" });
  },
}));
import { isEscapeOwnerOpen } from "@/lib/hotkeys";
import { NOTICE_MS } from "./layerParts";
import { DesktopDesignTools } from "./DesktopDesignTools";
import type { DesignToolsEvent } from "./desktopBridge";
import { resetDesignOutbox, useDesignOutbox } from "./core/designOutbox";
import { resetDesignTools, tabTools } from "./core/designToolsStore";

const VIEW = "conv_1";
const PNG = "data:image/png;base64,iVBORw0KGgo=";
const HIT = {
  element: { tag: "button", text: "Track order" },
  rect: { x: 10, y: 10, width: 80, height: 24 },
};

interface Call {
  op: string;
  session?: string;
  [key: string]: unknown;
}

// main's page operations (designTools.js), which the fake page answers.
const PAGE_OPS = new Set(["hitTest"]);

/** A desktop shell's design tools bridge: answers main's ops, and sends the view's end. */
function fakeShell() {
  const calls: Call[] = [];
  let listener: ((event: DesignToolsEvent) => void) | null = null;
  let openings = 0;
  const page = vi.fn(async (_call: Call): Promise<string> => "null");
  const capture = vi.fn(async (): Promise<object> => ({
    ok: true,
    png: PNG,
    width: 8,
    height: 8,
    viewport: { width: 800, height: 600 },
  }));
  const bridge = {
    browserDesignTools: vi.fn(async (args: Call) => {
      calls.push(args);
      if (args.op === "start") {
        openings += 1;
        return {
          ok: true,
          session: `open_${openings}`,
          frame: `omnigent-design-tools:open_${openings}`,
        };
      }
      if (args.op === "capture") return capture();
      if (PAGE_OPS.has(args.op)) {
        try {
          return { ok: true, result: await page(args) };
        } catch (error) {
          return { ok: false, error: String(error) };
        }
      }
      return { ok: true };
    }),
    onBrowserDesignToolsEvent: (callback: (event: DesignToolsEvent) => void) => {
      listener = callback;
      return () => {
        listener = null;
      };
    },
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return {
    bridge,
    calls,
    page,
    capture,
    ops: (op: string) => calls.filter((call) => call.op === op),
    emit: async (event: Omit<DesignToolsEvent, "viewId">) => {
      await act(async () => {
        listener?.({ viewId: VIEW, ...event });
      });
    },
  };
}

/** `window.open` as main lets it through: an overlay document (here, a frame's). */
function fakeOverlays({ denied = false } = {}) {
  const opened: { name: string; win: Window; close: ReturnType<typeof vi.fn> }[] = [];
  const openSpy = vi.spyOn(window, "open").mockImplementation((_url, name) => {
    if (denied) return null;
    const frame = document.createElement("iframe");
    document.body.append(frame);
    const win = frame.contentWindow!;
    const close = vi.fn();
    Object.defineProperty(win, "close", { value: close });
    opened.push({ name: String(name), win, close });
    return win;
  });
  return {
    open: openSpy,
    opened,
    /** The open overlay's document, to query. */
    view: (index = opened.length - 1) => within(opened[index]!.win.document.body),
  };
}

function renderTools({ room = true } = {}) {
  const container = createRef<HTMLDivElement>() as MutableRefObject<HTMLDivElement | null>;
  // jsdom lays nothing out: the pane's box has the size it's given here.
  const box = (element: HTMLDivElement | null) => {
    container.current = element;
    if (element) {
      element.getBoundingClientRect = () =>
        ({ width: room ? 800 : 0, height: room ? 600 : 0 }) as DOMRect;
    }
  };
  const tools = (active: boolean) => (
    <>
      <div ref={box} />
      <DesktopDesignTools
        viewId={VIEW}
        hasView
        active={active}
        url="https://example.com/"
        container={container}
      />
    </>
  );
  const rendered = render(tools(true));
  return { ...rendered, setActive: (active: boolean) => rendered.rerender(tools(active)) };
}

async function open(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
  });
  await settle();
}

const settle = () =>
  act(async () => {
    for (let turn = 0; turn < 5; turn += 1) {
      // oxlint-disable-next-line no-await-in-loop -- let queued work run, turn by turn
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    }
  });

const pressed = (name: string) =>
  screen.getByRole("button", { name }).getAttribute("aria-pressed") === "true";

beforeEach(() => {
  resetDesignTools();
  resetDesignOutbox();
  queued.mockClear();
  drawn.mockClear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const frame of document.querySelectorAll("iframe")) frame.remove();
  document.documentElement.classList.remove("dark");
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("DesktopDesignTools: closing from the tool's bar", () => {
  for (const [button, tool, close] of [
    ["Annotate an element", "annotate", "Close Annotate"],
    ["Draw on the page", "draw", "Close Draw"],
  ] as const) {
    it(`closes ${tool} with the x at the end of its bar`, async () => {
      fakeShell();
      const overlays = fakeOverlays();
      renderTools();
      await open(button);
      expect(tabTools(VIEW).tool).toBe(tool);
      await act(async () => {
        fireEvent.click(overlays.view().getByRole("button", { name: close }));
      });
      await settle();
      expect(tabTools(VIEW).tool).toBeNull();
    });
  }
});

describe("DesktopDesignTools: the overlay", () => {
  it("opens a tool's overlay as main names it, in the app's styles and theme", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    const style = document.createElement("style");
    style.textContent = ".app-style-probe{color:red}";
    document.head.append(style);
    // A stylesheet the app links by a path, which the overlay's blank document needs whole.
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.setAttribute("href", "/assets/app-probe.css");
    document.head.append(link);
    document.documentElement.classList.add("dark");
    try {
      renderTools();
      await open("Annotate an element");
      expect(shell.ops("start")).toEqual([expect.objectContaining({ input: true })]);
      expect(overlays.open).toHaveBeenCalledWith("about:blank", "omnigent-design-tools:open_1");
      const doc = overlays.opened[0]!.win.document;
      expect([...doc.head.querySelectorAll("style")].map((node) => node.textContent)).toContain(
        ".app-style-probe{color:red}",
      );
      expect(
        [...doc.head.querySelectorAll("link")].map((node) => node.getAttribute("href")),
      ).toEqual([new URL("/assets/app-probe.css", location.href).href]);
      expect(doc.documentElement).toHaveClass("dark");
      // The layer is in the overlay, not the app under the page.
      expect(overlays.view().getByTestId("annotate-layer")).toBeTruthy();
      expect(screen.queryByTestId("annotate-layer")).toBeNull();
    } finally {
      style.remove();
      link.remove();
    }
  });

  it("closes the overlay with its opening, and stops only that opening", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Annotate an element");
    await open("Annotate an element");
    expect(pressed("Annotate an element")).toBe(false);
    expect(overlays.opened[0]!.close).toHaveBeenCalled();
    expect(shell.ops("stop")).toEqual([expect.objectContaining({ session: "open_1" })]);
  });

  it("closes a tool whose overlay can't open, and says why in the toolbar if nothing can", async () => {
    const shell = fakeShell();
    fakeOverlays({ denied: true });
    renderTools();
    await open("Annotate an element");
    expect(pressed("Annotate an element")).toBe(false);
    // The tool's opening and the notice's: neither has an overlay, so both are stopped.
    expect(shell.ops("stop").map((call) => call.session)).toEqual(["open_1", "open_2"]);
    expect(screen.getByRole("status")).toHaveTextContent(
      "The tool couldn't open on this page. Try again once it has loaded.",
    );
  });

  it("keeps the toolbar's message its full time when the tool fails to open again", async () => {
    fakeShell();
    fakeOverlays({ denied: true });
    renderTools();
    const failed = "The tool couldn't open on this page. Try again once it has loaded.";
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await open("Annotate an element");
      await act(async () => {
        vi.advanceTimersByTime(NOTICE_MS - 1000);
      });
      await open("Annotate an element");
      // Past the first failure's time, though not the second's.
      await act(async () => {
        vi.advanceTimersByTime(2000);
      });
      expect(screen.getByRole("status")).toHaveTextContent(failed);
      await act(async () => {
        vi.advanceTimersByTime(NOTICE_MS);
      });
      expect(screen.queryByRole("status")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the same notice again its full time", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await open("Annotate an element");
      await shell.emit({ type: "ended", reason: "navigated" });
      await settle();
      await act(async () => {
        vi.advanceTimersByTime(NOTICE_MS - 1000);
      });
      await open("Annotate an element");
      await shell.emit({ type: "ended", reason: "navigated" });
      await settle();
      // Past the first notice's time, though not the second's.
      await act(async () => {
        vi.advanceTimersByTime(2000);
      });
      expect(overlays.view().getByRole("status")).toHaveTextContent(
        "The page changed, so the tool closed.",
      );
      await act(async () => {
        vi.advanceTimersByTime(NOTICE_MS);
      });
      expect(overlays.view().queryByRole("status")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a notice alone, in an overlay of its own size, when the page changed", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Annotate an element");
    await shell.emit({ type: "ended", reason: "navigated" });
    await settle();
    expect(pressed("Annotate an element")).toBe(false);
    expect(shell.ops("start").at(-1)).toEqual(expect.objectContaining({ input: false }));
    expect(overlays.view().getByRole("status")).toHaveTextContent(
      "The page changed, so the tool closed.",
    );
    // It wraps within the page, which its overlay was the size of when it measured.
    const { win } = overlays.opened.at(-1)!;
    expect(overlays.view().getByRole("status").style.maxWidth).toBe(`${win.innerWidth - 16}px`);
    expect(shell.ops("fit").at(-1)).toEqual(
      expect.objectContaining({
        notice: { width: expect.any(Number), height: expect.any(Number) },
      }),
    );
  });

  for (const [name, end] of [
    [
      "main ends a notice's opening",
      (shell: ReturnType<typeof fakeShell>) =>
        shell.emit({ type: "ended", session: "open_2", reason: "gone" }),
    ],
    [
      "a notice's overlay goes",
      async (_shell: ReturnType<typeof fakeShell>, overlays: ReturnType<typeof fakeOverlays>) => {
        await act(async () => {
          overlays.opened.at(-1)!.win.dispatchEvent(new Event("pagehide"));
        });
      },
    ],
  ] as const) {
    it(`shows the next notice afresh once ${name}`, async () => {
      const shell = fakeShell();
      const overlays = fakeOverlays();
      renderTools();
      await open("Annotate an element");
      await shell.emit({ type: "ended", reason: "navigated" });
      await settle();
      expect(shell.ops("start")).toHaveLength(2);
      await end(shell, overlays);
      await settle();
      // One of this tab's requests is sent: its notice opens an overlay of its own.
      const request = {
        id: "request_1",
        tabId: VIEW,
        conversationId: VIEW,
        label: "Annotate",
        text: "Make it green",
        files: [],
        status: "sending",
      } as const;
      act(() => useDesignOutbox.setState({ requests: [request] }));
      act(() => useDesignOutbox.setState({ requests: [{ ...request, status: "sent" }] }));
      await settle();
      expect(shell.ops("start")).toHaveLength(3);
      expect(overlays.view().getByRole("status")).toHaveTextContent("Sent to agent.");
    });
  }

  it("ignores an older opening's end once another has opened", async () => {
    const shell = fakeShell();
    fakeOverlays();
    renderTools();
    await open("Annotate an element");
    await open("Annotate an element");
    await open("Annotate an element");
    expect(shell.ops("start").map((call) => call.input)).toEqual([true, true]);
    // The first opening's end, arriving late.
    await shell.emit({ type: "ended", session: "open_1", reason: "navigated" });
    expect(pressed("Annotate an element")).toBe(true);
    await shell.emit({ type: "ended", session: "open_2", reason: "navigated" });
    expect(pressed("Annotate an element")).toBe(false);
  });

  it("closes the tool when a pane that had no box yet is squeezed to nothing", async () => {
    fakeShell();
    fakeOverlays();
    // Resize observers the test can fire (jsdom lays nothing out).
    const observers: { trigger: () => void }[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          observers.push({ trigger: callback });
        }
        observe() {}
        disconnect() {}
      },
    );
    const container = createRef<HTMLDivElement>() as MutableRefObject<HTMLDivElement | null>;
    let size = 800;
    const box = (element: HTMLDivElement | null) => {
      container.current = element;
      if (element) {
        element.getBoundingClientRect = () => ({ width: size, height: size }) as DOMRect;
      }
    };
    const tools = (withBox: boolean) => (
      <>
        {withBox && <div ref={box} />}
        <DesktopDesignTools
          viewId={VIEW}
          hasView={withBox}
          url="https://example.com/"
          container={container}
        />
      </>
    );
    // The pane's first page: its box comes after the tools' first render.
    const { rerender } = render(tools(false));
    rerender(tools(true));
    await open("Annotate an element");
    expect(pressed("Annotate an element")).toBe(true);
    size = 0;
    await act(async () => {
      for (const observer of observers) observer.trigger();
    });
    await settle();
    expect(pressed("Annotate an element")).toBe(false);
  });

  it("closes the tool when its overlay goes", async () => {
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    await act(async () => {
      overlays.opened[0]!.win.dispatchEvent(new Event("pagehide"));
    });
    expect(pressed("Draw on the page")).toBe(false);
    expect(tabTools(VIEW).tool).toBeNull();
  });

  it("closes the tool when the pane is hidden, and opens none in a pane with no room", async () => {
    fakeShell();
    fakeOverlays();
    const { setActive, unmount } = renderTools();
    await open("Annotate an element");
    act(() => setActive(false));
    expect(tabTools(VIEW).tool).toBeNull();
    unmount();
    renderTools({ room: false });
    expect(screen.getByRole("button", { name: "Annotate an element" })).toBeDisabled();
  });
});

describe("DesktopDesignTools: Annotate", () => {
  it("picks in the overlay and sends from its field, with the element where it is now", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    shell.page.mockImplementation(async () => JSON.stringify(HIT));
    renderTools();
    await open("Annotate an element");
    fireEvent.click(overlays.view().getByTestId("annotate-layer"), { clientX: 20, clientY: 30 });
    await settle();
    const field = overlays.view().getByLabelText("What should change?");
    fireEvent.change(field, { target: { value: "Make it green" } });
    await act(async () => {
      fireEvent.keyDown(field, { key: "Enter" });
    });
    await settle();
    // Picked there, and found there again before the screenshot.
    expect(shell.ops("hitTest").map(({ x, y }) => [x, y])).toEqual([
      [20, 30],
      [20, 30],
    ]);
    expect(shell.ops("capture")).toEqual([
      expect.objectContaining({ rect: { x: 2, y: 2, width: 96, height: 40 } }),
    ]);
    expect(queued).toHaveBeenCalledWith(
      expect.objectContaining({ files: [expect.objectContaining({ name: "design-element.png" })] }),
    );
    expect(overlays.opened[0]!.close).toHaveBeenCalled();
  });

  it("moves its card over the element once a long request makes it too tall to fit under", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    const low = { ...HIT, rect: { x: 20, y: 560, width: 100, height: 20 } };
    shell.page.mockImplementation(async () => JSON.stringify(low));
    renderTools();
    await open("Annotate an element");
    // jsdom lays nothing out: the card is as tall as a few lines of request make it.
    let cardHeight = 120;
    const { HTMLFormElement: OverlayForm } = overlays.opened[0]!.win as unknown as typeof window;
    Object.defineProperty(OverlayForm.prototype, "offsetHeight", {
      configurable: true,
      get: () => cardHeight,
    });
    fireEvent.click(overlays.view().getByTestId("annotate-layer"), { clientX: 30, clientY: 570 });
    await settle();
    const card = overlays.view().getByRole("form", { name: "Annotate" });
    expect(card.style.top).toBe("590px");
    cardHeight = 206;
    fireEvent.change(overlays.view().getByLabelText("What should change?"), {
      target: { value: "Make it green\nand bigger\nand bolder\nand rounder" },
    });
    await settle();
    expect(card.style.top).toBe("344px");
  });

  it("outlines and names the element under the pointer before it's picked", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    shell.page.mockImplementation(async () => JSON.stringify(HIT));
    renderTools();
    await open("Annotate an element");
    await act(async () => {
      fireEvent.pointerMove(overlays.view().getByTestId("annotate-layer"), {
        clientX: 20,
        clientY: 30,
      });
    });
    await settle();
    expect(shell.ops("hitTest")).toEqual([expect.objectContaining({ x: 20, y: 30 })]);
    expect(overlays.view().getByText('button "Track order"')).toBeInTheDocument();
  });

  it("outlines where the pointer stopped, not where it was when a slow hit test began", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    let answerFirst: (hit: string) => void = () => {};
    shell.page
      .mockImplementationOnce(
        () =>
          new Promise<string>((resolve) => {
            answerFirst = resolve;
          }),
      )
      .mockImplementation(async () =>
        JSON.stringify({ ...HIT, element: { tag: "a", text: "Help" } }),
      );
    renderTools();
    await open("Annotate an element");
    const layer = overlays.view().getByTestId("annotate-layer");
    await act(async () => {
      fireEvent.pointerMove(layer, { clientX: 20, clientY: 30 });
      fireEvent.pointerMove(layer, { clientX: 200, clientY: 300 });
    });
    // The first answers only after the pointer has stopped somewhere else.
    await act(async () => answerFirst(JSON.stringify(HIT)));
    await settle();
    expect(shell.ops("hitTest")).toEqual([
      expect.objectContaining({ x: 20, y: 30 }),
      expect.objectContaining({ x: 200, y: 300 }),
    ]);
    expect(overlays.view().getByText('a "Help"')).toBeInTheDocument();
  });
});

describe("DesktopDesignTools: Draw", () => {
  it("draws in the overlay to where the pointer is let go, and sends the page with the marks", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    const canvas = overlays.view().getByTestId("draw-layer");
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10 });
    // Let go in the overlay, without a move between.
    await act(async () => {
      fireEvent.pointerUp(overlays.opened[0]!.win, { clientX: 60, clientY: 40 });
    });
    expect(tabTools(VIEW).shapes).toEqual([
      expect.objectContaining({
        points: [
          { x: 10, y: 10 },
          { x: 60, y: 40 },
        ],
      }),
    ]);
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Send to agent" }));
    });
    await settle();
    expect(shell.ops("capture")).toHaveLength(1);
    expect(drawn).toHaveBeenCalledWith([expect.objectContaining({ kind: "pen" })], {
      width: 800,
      height: 600,
    });
    expect(queued).toHaveBeenCalledWith(
      expect.objectContaining({ files: [expect.objectContaining({ name: "design-drawing.png" })] }),
    );
  });

  it("keeps a stroke that loops back to its start, and draws only with the primary button", async () => {
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    const canvas = overlays.view().getByTestId("draw-layer");
    const { win } = overlays.opened[0]!;
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, button: 2 });
    fireEvent.pointerMove(canvas, { clientX: 50, clientY: 50 });
    await act(async () => {
      fireEvent.pointerUp(win, { clientX: 60, clientY: 60 });
    });
    expect(tabTools(VIEW).shapes).toEqual([]);
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, button: 0 });
    fireEvent.pointerMove(canvas, { clientX: 50, clientY: 10 });
    fireEvent.pointerMove(canvas, { clientX: 50, clientY: 50 });
    await act(async () => {
      fireEvent.pointerUp(win, { clientX: 10, clientY: 10 });
    });
    expect(tabTools(VIEW).shapes).toHaveLength(1);
    expect(tabTools(VIEW).shapes[0]!.points).toHaveLength(4);
  });

  it("draws at the display's current scale after the window changes displays", async () => {
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    const canvas = overlays.view().getByTestId("draw-layer") as HTMLCanvasElement;
    const { win } = overlays.opened[0]!;
    const methods: Record<PropertyKey, ReturnType<typeof vi.fn>> = {};
    const context = new Proxy(methods, { get: (target, key) => (target[key] ??= vi.fn()) });
    canvas.getContext = (() => context) as unknown as HTMLCanvasElement["getContext"];
    const cssWidth = parseFloat(canvas.style.width);
    const strokeAt = async (ratio: number) => {
      Object.defineProperty(win, "devicePixelRatio", { value: ratio, configurable: true });
      fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10 });
      await act(async () => {
        fireEvent.pointerUp(win, { clientX: 60, clientY: 40 });
      });
    };
    await strokeAt(2);
    expect(canvas.width).toBe(cssWidth * 2);
    expect(methods.setTransform).toHaveBeenLastCalledWith(2, 0, 0, 2, 0, 0);
    await strokeAt(1);
    expect(canvas.width).toBe(cssWidth);
    expect(methods.setTransform).toHaveBeenLastCalledWith(1, 0, 0, 1, 0, 0);
    await strokeAt(2);
    expect(canvas.width).toBe(cssWidth * 2);
    expect(methods.setTransform).toHaveBeenLastCalledWith(2, 0, 0, 2, 0, 0);
  });

  it("keeps Draw's text and the tool when Escape only cancels an input method's text", async () => {
    // Laid out, so the toolbar (and its Escape) counts as on screen.
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      {},
    ] as unknown as DOMRectList);
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    fireEvent.click(overlays.view().getByRole("button", { name: "Text" }));
    fireEvent.pointerDown(overlays.view().getByTestId("draw-layer"), {
      clientX: 100,
      clientY: 120,
    });
    const field = overlays.view().getByLabelText("Text to add");
    fireEvent.change(field, { target: { value: "ボタン" } });
    fireEvent.keyDown(field, { key: "Escape", isComposing: true });
    fireEvent.keyDown(field, { key: "Escape", keyCode: 229 });
    expect(overlays.view().getByLabelText("Text to add")).toHaveValue("ボタン");
    expect(pressed("Draw on the page")).toBe(true);
    fireEvent.keyDown(field, { key: "Escape" });
    expect(overlays.view().queryByLabelText("Text to add")).toBeNull();
    // With no text being added, the overlay's Escape closes the tool.
    expect(isEscapeOwnerOpen()).toBe(false);
    fireEvent.keyDown(overlays.opened[0]!.win, { key: "Escape" });
    expect(pressed("Draw on the page")).toBe(false);
  });

  it("leaves Escape typed in the app to whatever has focus there", async () => {
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      {},
    ] as unknown as DOMRectList);
    fakeShell();
    fakeOverlays();
    renderTools();
    const composerEscape = vi.fn((event: KeyboardEvent) => event.preventDefault());
    const composer = document.createElement("textarea");
    composer.addEventListener("keydown", composerEscape);
    document.body.append(composer);
    await open("Draw on the page");
    expect(isEscapeOwnerOpen()).toBe(false);
    fireEvent.keyDown(composer, { key: "Escape" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(composerEscape).toHaveBeenCalledTimes(1);
    expect(pressed("Draw on the page")).toBe(true);
    composer.remove();
  });

  it("closes the open tool on Escape from its focused toolbar button, ahead of the app's Escape", async () => {
    fakeShell();
    fakeOverlays();
    renderTools();
    await open("Draw on the page");
    const button = screen.getByRole("button", { name: "Draw on the page" });
    expect(isEscapeOwnerOpen()).toBe(false);
    act(() => button.focus());
    // The app's page-wide Escape (declining an approval, say) leaves it to the button.
    expect(isEscapeOwnerOpen()).toBe(true);
    const appEscape = vi.fn();
    window.addEventListener("keydown", appEscape);
    fireEvent.keyDown(button, { key: "Escape" });
    window.removeEventListener("keydown", appEscape);
    expect(appEscape).not.toHaveBeenCalled();
    expect(pressed("Draw on the page")).toBe(false);
    expect(isEscapeOwnerOpen()).toBe(false);
  });

  it("sends the wheel on to the page with the keys held", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Annotate an element");
    fireEvent.wheel(overlays.view().getByTestId("annotate-layer"), {
      clientX: 20,
      clientY: 30,
      deltaY: 40,
      ctrlKey: true,
    });
    await settle();
    expect(shell.ops("scroll").at(-1)).toEqual(
      expect.objectContaining({ x: 20, y: 30, dy: 40, modifiers: ["control"] }),
    );
  });

  it("opens its colors in the overlay", async () => {
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Color for Pen" }));
    });
    expect(await overlays.view().findByRole("button", { name: "Blue" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Blue" })).toBeNull();
  });

  it("puts the words being typed on the canvas before Send", async () => {
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    await open("Draw on the page");
    fireEvent.click(overlays.view().getByRole("button", { name: "Text" }));
    fireEvent.pointerDown(overlays.view().getByTestId("draw-layer"), {
      clientX: 100,
      clientY: 120,
    });
    fireEvent.change(overlays.view().getByLabelText("Text to add"), {
      target: { value: "Hello" },
    });
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Send to agent" }));
    });
    await settle();
    expect(drawn).toHaveBeenCalledWith(
      [expect.objectContaining({ kind: "text", text: "Hello" })],
      expect.anything(),
    );
  });
});
