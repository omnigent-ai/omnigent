import type { IBufferLine, IDisposable, Terminal } from "@xterm/xterm";

const RTL_TEXT = /[\p{Script=Hebrew}\p{Script=Arabic}]/u;

/** Convert DOM text offsets to terminal columns, including wide and combined cells. */
export function terminalColumnAtOffset(line: IBufferLine, offset: number): number {
  let textOffset = 0;
  for (let column = 0; column < line.length; column++) {
    const cell = line.getCell(column);
    if (!cell || cell.getWidth() === 0) continue;
    if (textOffset >= offset) return column;
    textOffset += cell.getChars().length || 1;
    if (textOffset > offset) return column;
  }
  return line.length;
}

/** Use browser paragraph layout without changing the terminal's logical buffer. */
export class TerminalBidi implements IDisposable {
  private readonly subscriptions: IDisposable[];
  private readonly events = new AbortController();
  private enabled = false;
  private dragging = false;

  private readonly terminal: Terminal;
  private readonly useDomRenderer: () => void;

  constructor(terminal: Terminal, useDomRenderer: () => void) {
    this.terminal = terminal;
    this.useDomRenderer = useDomRenderer;
    this.subscriptions = [
      terminal.onWriteParsed(() => this.detect()),
      terminal.onRender(() => {
        this.detect();
        this.layoutRows();
      }),
    ];
    const element = terminal.element;
    if (!element) throw new Error("Open the terminal before enabling bidirectional layout");
    for (const type of [
      "mousedown",
      "mousemove",
      "mouseup",
      "click",
      "dblclick",
      "contextmenu",
      "wheel",
    ]) {
      element.ownerDocument.addEventListener(
        type,
        (event) => this.projectMouse(event as MouseEvent),
        {
          capture: true,
          signal: this.events.signal,
        },
      );
    }
  }

  private detect(): void {
    if (this.enabled) return;
    const buffer = this.terminal.buffer.active;
    for (let row = 0; row < this.terminal.rows; row++) {
      if (RTL_TEXT.test(buffer.getLine(buffer.viewportY + row)?.translateToString(true) ?? "")) {
        this.enabled = true;
        this.useDomRenderer();
        this.terminal.element?.classList.add("xterm-bidi");
        this.layoutRows();
        this.terminal.refresh(0, this.terminal.rows - 1);
        return;
      }
    }
  }

  private layoutRows(): void {
    if (!this.enabled) return;
    for (const row of this.terminal.element?.querySelectorAll<HTMLElement>(".xterm-rows > div") ??
      []) {
      row.dir = RTL_TEXT.test(row.textContent ?? "") ? "auto" : "ltr";
    }
  }

  private projectMouse(event: MouseEvent): void {
    const element = this.terminal.element;
    if (!this.enabled || !element) return;
    const inside = event.target instanceof Node && element.contains(event.target);
    if (event.type === "mousedown") this.dragging = inside && event.button === 0;
    const owned = inside || this.dragging;
    if (event.type === "mouseup") this.dragging = false;
    if (!owned) return;
    const rows = element.querySelectorAll<HTMLElement>(".xterm-rows > div");
    const screen = element.querySelector(".xterm-screen");
    if (!screen || rows.length === 0) return;
    const bounds = screen.getBoundingClientRect();
    if (bounds.width === 0 || bounds.height === 0) return;
    const index = Math.max(
      0,
      Math.min(
        rows.length - 1,
        Math.floor((event.clientY - bounds.top) / (bounds.height / this.terminal.rows)),
      ),
    );
    const row = rows[index];
    if (row.dir !== "auto") return;
    const document = element.ownerDocument;
    const x = Math.max(bounds.left + 1, Math.min(bounds.right - 1, event.clientX));
    const rect = row.getBoundingClientRect();
    const y = Math.max(rect.top + 1, Math.min(rect.bottom - 1, event.clientY));
    // Keep xterm's stable mouse target; expose text only for the caret hit test.
    const rowsHost = row.parentElement!;
    const pointerEvents = rowsHost.style.pointerEvents;
    let node: Node | undefined;
    let offset: number | undefined;
    try {
      rowsHost.style.pointerEvents = "auto";
      const caret = document.caretPositionFromPoint?.(x, y);
      const fallback = !caret ? document.caretRangeFromPoint?.(x, y) : null;
      node = caret?.offsetNode ?? fallback?.startContainer;
      offset = caret?.offset ?? fallback?.startOffset;
    } finally {
      rowsHost.style.pointerEvents = pointerEvents;
    }
    if (!node || offset === undefined || !row.contains(node)) return;
    const prefix = document.createRange();
    prefix.setStart(row, 0);
    prefix.setEnd(node, offset);
    const line = this.terminal.buffer.active.getLine(this.terminal.buffer.active.viewportY + index);
    if (!line) return;
    const column = terminalColumnAtOffset(line, prefix.toString().length);
    // Preserve the original trusted event, including buttons, modifiers and drag ownership.
    Object.defineProperty(event, "clientX", {
      configurable: true,
      value: bounds.left + ((column + 0.01) * bounds.width) / this.terminal.cols,
    });
  }

  dispose(): void {
    this.events.abort();
    for (const subscription of this.subscriptions) subscription.dispose();
    this.terminal.element?.classList.remove("xterm-bidi");
    for (const row of this.terminal.element?.querySelectorAll(".xterm-rows > div") ?? []) {
      row.removeAttribute("dir");
    }
  }
}
