// Live Edit on the desktop Browser pane: its layer in the overlay, and the page operations
// it asks main for, in the Live Edit scope main hands out.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { createRef, type MutableRefObject } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as OutboxModule from "./core/designOutbox";

const queued = vi.hoisted(() => vi.fn((_request: { label: string; text: string }) => "id"));
vi.mock("./core/designOutbox", async (importOriginal) => ({
  ...(await importOriginal<typeof OutboxModule>()),
  queueDesignRequest: queued,
}));
import { DesktopDesignTools } from "./DesktopDesignTools";
import { resetDesignOutbox } from "./core/designOutbox";
import { resetDesignTools, tabTools } from "./core/designToolsStore";

const VIEW = "conv_1";
const PICK = {
  handle: 1,
  before: "Old title",
  text: "Old title",
  caret: 4,
  element: { tag: "h1", id: "#title" },
  rect: { x: 10, y: 10, width: 120, height: 24 },
};

interface Call {
  op: string;
  [key: string]: unknown;
}

/** A desktop shell whose page answers Live Edit's operations with `page`. */
function fakeShell() {
  const calls: Call[] = [];
  let openings = 0;
  let scopes = 0;
  const page = vi.fn(async (call: Call): Promise<string> => {
    if (call.op === "liveEditPick") return JSON.stringify(PICK);
    if (call.op === "liveEditRestore") return "1";
    return "ok";
  });
  const end = vi.fn(async (_call: Call): Promise<object> => ({ ok: true, result: "" }));
  const bridge = {
    browserDesignTools: vi.fn(async (args: Call) => {
      calls.push(args);
      if (args.op === "start") {
        openings += 1;
        return { ok: true, session: `open_${openings}`, frame: `frame_${openings}` };
      }
      if (args.op === "liveEditBegin") return { ok: true, scope: `scope_${++scopes}` };
      if (args.op === "liveEditEnd") return end(args);
      if (args.op.startsWith("liveEdit")) return { ok: true, result: await page(args) };
      return { ok: true };
    }),
    onBrowserDesignToolsEvent: () => () => {},
  };
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = bridge;
  return { page, end, ops: (op: string) => calls.filter((call) => call.op === op) };
}

/** `window.open` as main lets it through: an overlay document (here, a frame's). */
function fakeOverlays() {
  const opened: Window[] = [];
  vi.spyOn(window, "open").mockImplementation(() => {
    const frame = document.createElement("iframe");
    document.body.append(frame);
    const win = frame.contentWindow!;
    Object.defineProperty(win, "close", { value: vi.fn() });
    opened.push(win);
    return win;
  });
  return { view: () => within(opened.at(-1)!.document.body), win: () => opened.at(-1)! };
}

function renderTools() {
  const container = createRef<HTMLDivElement>() as MutableRefObject<HTMLDivElement | null>;
  const box = (element: HTMLDivElement | null) => {
    container.current = element;
    if (element) element.getBoundingClientRect = () => ({ width: 800, height: 600 }) as DOMRect;
  };
  render(
    <>
      <div ref={box} />
      <DesktopDesignTools
        viewId={VIEW}
        hasView
        url="https://app.example.com/orders/12?token=t0k3n"
        container={container}
      />
    </>,
  );
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

async function open(name: string) {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
  });
  await settle();
}

/** Open Live Edit and pick the page's text. */
async function pickText(overlays: ReturnType<typeof fakeOverlays>) {
  await open("Live Edit text");
  fireEvent.click(overlays.view().getByTestId("live-edit-layer"), { clientX: 30, clientY: 40 });
  await settle();
  return overlays.view().getByLabelText("Edit text") as HTMLTextAreaElement;
}

const pressed = (name: string) =>
  screen.getByRole("button", { name }).getAttribute("aria-pressed") === "true";

beforeEach(() => {
  resetDesignTools();
  resetDesignOutbox();
  queued.mockClear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  for (const frame of document.querySelectorAll("iframe")) frame.remove();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("DesktopDesignTools: Live Edit", () => {
  it("picks and previews in main's scope, and puts the page back on Discard", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    const editor = await pickText(overlays);
    expect(shell.ops("liveEditPick")).toEqual([
      expect.objectContaining({ session: "open_1", scope: "scope_1", x: 30, y: 40 }),
    ]);
    // The cursor starts where the click landed in the text.
    expect(editor.value).toBe("Old title");
    expect(editor.selectionStart).toBe(4);
    fireEvent.change(editor, { target: { value: "New title" } });
    await settle();
    expect(shell.ops("liveEditPreview")).toEqual([
      expect.objectContaining({ scope: "scope_1", handle: 1, text: "New title" }),
    ]);
    expect(overlays.view().getByText("1 edit ready")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Discard" }));
    });
    await settle();
    expect(shell.ops("liveEditRestore")).toHaveLength(1);
    expect(pressed("Live Edit text")).toBe(false);
    expect(shell.ops("liveEditEnd")).toEqual([
      expect.objectContaining({ scope: "scope_1", texts: null }),
    ]);
    expect(shell.ops("liveEditEnd")[0]).not.toHaveProperty("restore");
  });

  it("sends the edits to the agent with the old and new text, and keeps them on the page", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    fireEvent.change(await pickText(overlays), { target: { value: "New title" } });
    await settle();
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Send to agent" }));
    });
    await settle();
    expect(queued).toHaveBeenCalledTimes(1);
    const { label, text } = queued.mock.calls[0]![0];
    expect(label).toBe("Live Edit: 1 edit");
    expect(text).toContain('1. <h1>#title: "Old title" → "New title"');
    expect(text).toContain("Page: https://app.example.com\n");
    expect(text).not.toContain("t0k3n");
    expect(pressed("Live Edit text")).toBe(false);
    expect(shell.ops("liveEditEnd")).toEqual([
      expect.objectContaining({ scope: "scope_1", texts: [[1, "New title"]] }),
    ]);
  });

  it("still puts the page back when another tool replaces it during Discard", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    fireEvent.change(await pickText(overlays), { target: { value: "New title" } });
    await settle();
    let release = () => {};
    shell.page.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve("1");
        }),
    );
    await act(async () => {
      fireEvent.click(overlays.view().getByRole("button", { name: "Discard" }));
    });
    await open("Annotate an element");
    await act(async () => release());
    await settle();
    expect(shell.ops("liveEditEnd")).toEqual([
      expect.objectContaining({ scope: "scope_1", restore: true }),
    ]);
  });

  it("ends with the latest text, for a preview still queued when it closed", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    const editor = await pickText(overlays);
    let release = () => {};
    shell.page.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve("ok");
        }),
    );
    fireEvent.change(editor, { target: { value: "First" } });
    await settle();
    fireEvent.change(editor, { target: { value: "Latest" } });
    await open("Live Edit text");
    await act(async () => release());
    await settle();
    expect(shell.ops("liveEditPreview").map((call) => call.text)).toEqual(["First"]);
    expect(shell.ops("liveEditEnd")).toEqual([
      expect.objectContaining({ scope: "scope_1", texts: [[1, "Latest"]] }),
    ]);
  });

  it("closes its editor on Escape, and then the tool", async () => {
    // Laid out, so the toolbar (and its Escape) counts as on screen.
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([
      {},
    ] as unknown as DOMRectList);
    fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    const editor = await pickText(overlays);
    await act(async () => {
      fireEvent.keyDown(editor, { key: "Escape" });
    });
    expect(overlays.view().queryByLabelText("Edit text")).toBeNull();
    expect(pressed("Live Edit text")).toBe(true);
    await act(async () => {
      fireEvent.keyDown(overlays.win(), { key: "Escape" });
    });
    expect(tabTools(VIEW).tool).toBeNull();
  });

  it("asks main once more for an end the page didn't take, and says when it still doesn't", async () => {
    const shell = fakeShell();
    const overlays = fakeOverlays();
    renderTools();
    shell.end.mockResolvedValue({ ok: false, error: "the page didn't answer" });
    fireEvent.change(await pickText(overlays), { target: { value: "New" } });
    await settle();
    await open("Live Edit text");
    await settle();
    const ends = shell.ops("liveEditEnd");
    expect(ends).toHaveLength(2);
    expect(ends[1]).toEqual(ends[0]);
    // The tool has closed: the notice shows in an overlay of its own.
    expect(
      overlays
        .view()
        .getByText(
          "The page didn't take the tool's last change, so it may not show your latest edit.",
        ),
    ).toBeInTheDocument();
  });
});
