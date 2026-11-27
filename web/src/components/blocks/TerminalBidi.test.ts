import { Terminal } from "@xterm/xterm";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TerminalBidi, terminalColumnAtOffset } from "./TerminalBidi";

const terminals: Terminal[] = [];
const layouts: TerminalBidi[] = [];
const containers: HTMLElement[] = [];
afterEach(() => {
  for (const layout of layouts.splice(0)) layout.dispose();
  for (const terminal of terminals.splice(0)) terminal.dispose();
  for (const container of containers.splice(0)) container.remove();
  vi.restoreAllMocks();
});

function openTerminal() {
  const terminal = new Terminal({ cols: 40, rows: 4, allowProposedApi: true });
  const container = document.createElement("div");
  document.body.appendChild(container);
  containers.push(container);
  terminal.open(container);
  terminals.push(terminal);
  return terminal;
}

async function write(terminal: Terminal, text: string) {
  await new Promise<void>((resolve) => {
    terminal.write(text, resolve);
  });
}

describe("TerminalBidi", () => {
  it("keeps the GPU for LTR text and switches once when RTL arrives", async () => {
    const terminal = openTerminal();
    const useDom = vi.fn();
    const layout = new TerminalBidi(terminal, useDom);
    layouts.push(layout);
    await write(terminal, "git status\r\n");
    expect(useDom).not.toHaveBeenCalled();
    await write(terminal, "שלום עולם\r\n");
    expect(useDom).toHaveBeenCalledOnce();
    expect(terminal.element).toHaveClass("xterm-bidi");
    await write(terminal, "مرحبا بالعالم\r\n");
    expect(useDom).toHaveBeenCalledOnce();
    expect(terminal.buffer.active.getLine(1)?.translateToString(true)).toBe("שלום עולם");
    layout.dispose();
    expect(terminal.element).not.toHaveClass("xterm-bidi");
  });

  it("translates wide, combining and surrogate characters into grid columns", async () => {
    const terminal = openTerminal();
    await write(terminal, "A界שָ\u{1d11e}B");
    const line = terminal.buffer.active.getLine(0)!;
    expect([0, 1, 2, 3, 4, 5, 6, 7].map((offset) => terminalColumnAtOffset(line, offset))).toEqual([
      0, 1, 3, 3, 4, 4, 5, 6,
    ]);
  });

  it("projects a visual caret into the original mouse event and restores hit testing", async () => {
    const terminal = openTerminal();
    layouts.push(new TerminalBidi(terminal, vi.fn()));
    await write(terminal, "שלום");
    const screen = terminal.element!.querySelector<HTMLElement>(".xterm-screen")!;
    const row = terminal.element!.querySelector<HTMLElement>(".xterm-rows > div")!;
    const text = document.createTextNode("שלום");
    row.replaceChildren(text);
    row.dir = "auto";
    const rect = { left: 10, right: 410, top: 20, bottom: 100, width: 400, height: 80 } as DOMRect;
    vi.spyOn(screen, "getBoundingClientRect").mockReturnValue(rect);
    vi.spyOn(row, "getBoundingClientRect").mockReturnValue({ ...rect, bottom: 40, height: 20 });
    const original = Object.getOwnPropertyDescriptor(document, "caretPositionFromPoint");
    Object.defineProperty(document, "caretPositionFromPoint", {
      configurable: true,
      value: () => {
        expect(row.parentElement!.style.pointerEvents).toBe("auto");
        return { offsetNode: text, offset: 2 };
      },
    });
    try {
      const event = new MouseEvent("mousedown", {
        bubbles: true,
        clientX: 390,
        clientY: 30,
        ctrlKey: true,
      });
      screen.dispatchEvent(event);
      expect(event.clientX).toBeCloseTo(30.1);
      expect(event.clientY).toBe(30);
      expect(event.ctrlKey).toBe(true);
      expect(row.parentElement!.style.pointerEvents).toBe("");
    } finally {
      if (original) Object.defineProperty(document, "caretPositionFromPoint", original);
      else delete (document as { caretPositionFromPoint?: unknown }).caretPositionFromPoint;
    }
  });

  it("stops reacting to later output after disposal", async () => {
    const terminal = openTerminal();
    const useDom = vi.fn();
    const layout = new TerminalBidi(terminal, useDom);
    layout.dispose();
    await write(terminal, "שלום");
    expect(useDom).not.toHaveBeenCalled();
  });
});
