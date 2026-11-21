import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import {
  isTerminalClipboardWritePending,
  queueTerminalClipboardWrite,
} from "@/lib/terminalClipboardWriter";
import {
  TERMINAL_COPY_BLOCKED_MESSAGE,
  type TerminalPasteResult,
  type TerminalPasteTarget,
  TerminalTouchClipboardBar,
} from "./TerminalTouchClipboard";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: clipboardMock.copyText }));

const readText = vi.fn<() => Promise<string>>();
const TERMINAL_TEXT = "$ ls\nalpha.txt\nbeta.txt";

type BarProps = Parameters<typeof TerminalTouchClipboardBar>[0];

function renderBar(overrides: Partial<BarProps> = {}) {
  const props: BarProps = {
    pasteAvailable: true,
    pasteEnabled: true,
    readText: vi.fn(() => TERMINAL_TEXT),
    isCopyBlocked: vi.fn(() => false),
    startPaste: vi.fn(() => (): TerminalPasteTarget => "current"),
    onPaste: vi.fn<(text: string, reviewed: boolean) => TerminalPasteResult>(() => "pasted"),
    ...overrides,
  };
  const view = (barProps: BarProps) => (
    <>
      <Toaster />
      <TerminalTouchClipboardBar {...barProps} />
    </>
  );
  const { rerender } = render(view(props));
  return { props, rerender: (next: Partial<BarProps>) => rerender(view({ ...props, ...next })) };
}

function selectText(node: Node, start: number, end: number, endNode: Node = node): void {
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(endNode, end);
  act(() => {
    document.getSelection()!.removeAllRanges();
    document.getSelection()!.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
}

async function openPasteBox() {
  fireEvent.click(screen.getByRole("button", { name: "Paste" }));
  return screen.findByTestId("terminal-paste-box");
}

beforeEach(() => {
  clipboardMock.copyText.mockReset().mockResolvedValue(undefined);
  readText.mockReset();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { readText },
  });
});

afterEach(() => {
  act(() => toast.dismiss());
  cleanup();
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("TerminalTouchClipboardBar copy", () => {
  it("copies only the selected part of the text", async () => {
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");

    selectText(text.firstChild!, 5, 14);
    // A tap on the button may collapse the selection outside the text first.
    act(() => {
      document.getSelection()!.removeAllRanges();
      document.dispatchEvent(new Event("selectionchange"));
    });

    fireEvent.click(screen.getByRole("button", { name: "Copy selection" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("alpha.txt"));
  });

  it("ignores a selection that runs past the terminal text", async () => {
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");
    const footerLabel = screen.getByRole("button", { name: "Copy all" }).lastChild!;

    selectText(text.firstChild!, 5, 2, footerLabel);

    fireEvent.click(screen.getByRole("button", { name: "Copy all" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith(TERMINAL_TEXT));
  });

  it("forgets a selection dragged out of the text", async () => {
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");
    selectText(text.firstChild!, 5, 14);
    expect(screen.getByRole("button", { name: "Copy selection" })).toBeTruthy();

    const closeLabel = screen.getByRole("button", { name: "Copy selection" }).lastChild!;
    selectText(text.firstChild!, 5, 2, closeLabel);

    fireEvent.click(screen.getByRole("button", { name: "Copy all" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith(TERMINAL_TEXT));
  });

  it("keeps the sheet open and explains a failed copy", async () => {
    clipboardMock.copyText.mockRejectedValue(new Error("denied"));
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy all" }));

    expect(
      await screen.findByText("Couldn't copy. Select the text and choose Copy from the menu."),
    ).toBeTruthy();
    expect(screen.getByTestId("terminal-copy-sheet")).toBeTruthy();
  });

  it("stops copying when the clipboard is blocked while the sheet is open", async () => {
    let blocked = false;
    renderBar({ isCopyBlocked: () => blocked });
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");

    blocked = true;
    const nativeCopy = new Event("copy", { bubbles: true, cancelable: true });
    act(() => {
      text.dispatchEvent(nativeCopy);
    });
    fireEvent.click(screen.getByRole("button", { name: "Copy all" }));

    expect(nativeCopy.defaultPrevented).toBe(true);
    expect(await screen.findByText(TERMINAL_COPY_BLOCKED_MESSAGE)).toBeTruthy();
    expect(clipboardMock.copyText).not.toHaveBeenCalled();
  });

  it("does not restore a copy blocked while an older program copy finishes", async () => {
    let finishFirst!: () => void;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishFirst = resolve;
        }),
    );
    queueTerminalClipboardWrite({ text: "first", isCurrent: () => true, onResult: () => {} });
    let blocked = false;
    renderBar({ isCopyBlocked: () => blocked });
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy all" }));
    await waitFor(() => expect(screen.queryByTestId("terminal-copy-sheet")).toBeNull());

    blocked = true;
    await act(async () => finishFirst());

    await waitFor(() => expect(isTerminalClipboardWritePending()).toBe(false));
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"], [TERMINAL_TEXT]]);
  });

  it("keeps a reopened sheet open when an earlier copy finishes late", async () => {
    let finishCopy!: () => void;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishCopy = resolve;
        }),
    );
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy all" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Close" })[0]);
    await waitFor(() => expect(screen.queryByTestId("terminal-copy-sheet")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await screen.findByTestId("terminal-copy-sheet");

    await act(async () => finishCopy());

    expect(await screen.findByText("Copied terminal text")).toBeTruthy();
    expect(screen.getByTestId("terminal-copy-sheet")).toBeTruthy();
  });

  it("is not overwritten by a program copy queued before it", async () => {
    let finishFirst!: () => void;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishFirst = resolve;
        }),
    );
    queueTerminalClipboardWrite({ text: "first", isCurrent: () => true, onResult: () => {} });
    queueTerminalClipboardWrite({ text: "queued", isCurrent: () => true, onResult: () => {} });
    renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Copy all" }));
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"], [TERMINAL_TEXT]]);
    await waitFor(() => expect(screen.queryByTestId("terminal-copy-sheet")).toBeNull());
    await act(async () => finishFirst());

    // The older copy finished last, so the chosen text is written again.
    await waitFor(() => expect(isTerminalClipboardWritePending()).toBe(false));
    expect(clipboardMock.copyText.mock.calls).toEqual([
      ["first"],
      [TERMINAL_TEXT],
      [TERMINAL_TEXT],
    ]);
  });

  it("keeps a native copy from being overwritten by program copies", async () => {
    let finishFirst!: () => void;
    clipboardMock.copyText.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishFirst = resolve;
        }),
    );
    queueTerminalClipboardWrite({ text: "first", isCurrent: () => true, onResult: () => {} });
    queueTerminalClipboardWrite({ text: "queued", isCurrent: () => true, onResult: () => {} });
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");
    selectText(text.firstChild!, 5, 14);

    const nativeCopy = new Event("copy", { bubbles: true, cancelable: true });
    act(() => {
      text.dispatchEvent(nativeCopy);
    });
    finishFirst();

    expect(nativeCopy.defaultPrevented).toBe(false);
    await waitFor(() => expect(isTerminalClipboardWritePending()).toBe(false));
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"], ["alpha.txt"], ["alpha.txt"]]);
  });
});

describe("TerminalTouchClipboardBar paste", () => {
  it("says so when the clipboard has no text", async () => {
    readText.mockResolvedValue("");
    const { props } = renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    expect(await screen.findByText("The clipboard has no text to paste.")).toBeTruthy();
    expect(props.onPaste).not.toHaveBeenCalled();
  });

  it("says so when the terminal disconnects before the clipboard is read", async () => {
    readText.mockResolvedValue("ls");
    const { props } = renderBar({ onPaste: vi.fn((): TerminalPasteResult => "unavailable") });

    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    expect(
      await screen.findByText("The terminal isn't connected, so nothing was pasted."),
    ).toBeTruthy();
    expect(props.onPaste).toHaveBeenCalledWith("ls", false);
    expect(screen.queryByTestId("terminal-paste-box")).toBeNull();
  });

  it.each([
    ["disconnected", "The terminal isn't connected, so nothing was pasted."],
    ["changed", "The terminal changed while the clipboard was read, so nothing was pasted."],
  ] as const)(
    "drops a clipboard read that finishes after the terminal %s",
    async (target, message) => {
      readText.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
      const { props } = renderBar({ startPaste: () => () => target });

      fireEvent.click(screen.getByRole("button", { name: "Paste" }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(props.onPaste).not.toHaveBeenCalled();
      expect(screen.queryByTestId("terminal-paste-box")).toBeNull();
    },
  );

  it.each([
    ["refused", () => readText.mockRejectedValue(new DOMException("denied", "NotAllowedError"))],
    ["unavailable", () => Reflect.deleteProperty(navigator, "clipboard")],
  ])("offers a paste box when clipboard reads are %s", async (_name, arrange) => {
    arrange();
    const { props } = renderBar();

    const box = await openPasteBox();
    const send = within(box).getByRole("button", { name: "Send to terminal" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(box).getByLabelText("Text to paste"), {
      target: { value: "make test" },
    });
    fireEvent.click(send);

    expect(props.onPaste).toHaveBeenCalledWith("make test", true);
    await waitFor(() => expect(screen.queryByTestId("terminal-paste-box")).toBeNull());
  });

  it("keeps the paste box text when the terminal can't take it", async () => {
    readText.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    renderBar({ onPaste: vi.fn((): TerminalPasteResult => "unavailable") });

    const box = await openPasteBox();
    fireEvent.change(within(box).getByLabelText("Text to paste"), {
      target: { value: "make test" },
    });
    fireEvent.click(within(box).getByRole("button", { name: "Send to terminal" }));

    expect(
      await screen.findByText("The terminal isn't connected, so nothing was pasted."),
    ).toBeTruthy();
    expect(within(box).getByLabelText("Text to paste")).toHaveValue("make test");
  });

  it("asks for a review before pasting lines that would each run", async () => {
    readText.mockResolvedValue("make\nmake test");
    const onPaste = vi.fn((_text: string, reviewed: boolean): TerminalPasteResult =>
      reviewed ? "pasted" : "review",
    );
    renderBar({ onPaste });

    const box = await openPasteBox();
    expect(within(box).getByLabelText("Text to paste")).toHaveValue("make\nmake test");
    expect(within(box).getByText(/may run as a command, and a tab may complete it/)).toBeTruthy();
    fireEvent.click(within(box).getByRole("button", { name: "Send to terminal" }));

    expect(onPaste.mock.calls).toEqual([
      ["make\nmake test", false],
      ["make\nmake test", true],
    ]);
    await waitFor(() => expect(screen.queryByTestId("terminal-paste-box")).toBeNull());
  });

  it("ignores a clipboard read that finishes after the terminal turns read-only", async () => {
    let refuseRead!: () => void;
    readText.mockReturnValue(
      new Promise<string>((_resolve, reject) => {
        refuseRead = () => reject(new DOMException("denied", "NotAllowedError"));
      }),
    );
    const { props, rerender } = renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    rerender({ pasteAvailable: false });
    await act(async () => refuseRead());
    rerender({ pasteAvailable: true });

    expect(screen.queryByTestId("terminal-paste-box")).toBeNull();
    expect(props.onPaste).not.toHaveBeenCalled();
  });

  it("does not reopen the paste box after the terminal turns read-only", async () => {
    readText.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    const { rerender } = renderBar();
    await openPasteBox();

    rerender({ pasteAvailable: false });
    await waitFor(() => expect(screen.queryByTestId("terminal-paste-box")).toBeNull());
    rerender({ pasteAvailable: true });

    expect(screen.getByRole("button", { name: "Paste" })).toBeTruthy();
    expect(screen.queryByTestId("terminal-paste-box")).toBeNull();
  });
});
