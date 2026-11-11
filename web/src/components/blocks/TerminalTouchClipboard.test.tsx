import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { TERMINAL_COPY_BLOCKED_MESSAGE, TerminalTouchClipboardBar } from "./TerminalTouchClipboard";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: clipboardMock.copyText }));

const readText = vi.fn<() => Promise<string>>();

function renderBar(overrides: Partial<Parameters<typeof TerminalTouchClipboardBar>[0]> = {}) {
  const props = {
    pasteAvailable: true,
    pasteEnabled: true,
    readText: vi.fn(() => "$ ls\nalpha.txt\nbeta.txt"),
    isCopyBlocked: vi.fn(() => false),
    onPaste: vi.fn<(text: string) => void>(),
    ...overrides,
  };
  render(
    <>
      <Toaster />
      <TerminalTouchClipboardBar {...props} />
    </>,
  );
  return props;
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
  it("shows the terminal text and copies all of it", async () => {
    const props = renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    const sheet = await screen.findByTestId("terminal-copy-sheet");
    expect(within(sheet).getByTestId("terminal-copy-text").textContent).toBe(
      "$ ls\nalpha.txt\nbeta.txt",
    );
    expect(props.readText).toHaveBeenCalledOnce();
    fireEvent.click(within(sheet).getByRole("button", { name: "Copy all" }));
    await waitFor(() =>
      expect(clipboardMock.copyText).toHaveBeenCalledWith("$ ls\nalpha.txt\nbeta.txt"),
    );
    await waitFor(() => expect(screen.queryByTestId("terminal-copy-sheet")).toBeNull());
    expect(await screen.findByText("Copied terminal text")).toBeTruthy();
  });

  it("copies only the selected part of the text", async () => {
    renderBar();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    const text = await screen.findByTestId("terminal-copy-text");

    const range = document.createRange();
    const node = text.firstChild!;
    range.setStart(node, 5);
    range.setEnd(node, 14);
    act(() => {
      document.getSelection()!.removeAllRanges();
      document.getSelection()!.addRange(range);
      document.dispatchEvent(new Event("selectionchange"));
    });
    // A tap on the button may collapse the selection outside the text first.
    act(() => {
      document.getSelection()!.removeAllRanges();
      document.dispatchEvent(new Event("selectionchange"));
    });

    fireEvent.click(screen.getByRole("button", { name: "Copy selection" }));
    await waitFor(() => expect(clipboardMock.copyText).toHaveBeenCalledWith("alpha.txt"));
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

  it("honors a blocked terminal clipboard choice", async () => {
    const props = renderBar({ isCopyBlocked: vi.fn(() => true) });

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(await screen.findByText(TERMINAL_COPY_BLOCKED_MESSAGE)).toBeTruthy();
    expect(screen.queryByTestId("terminal-copy-sheet")).toBeNull();
    expect(props.readText).not.toHaveBeenCalled();
  });
});

describe("TerminalTouchClipboardBar paste", () => {
  it("sends clipboard text to the terminal", async () => {
    readText.mockResolvedValue("git status\n");
    const props = renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    await waitFor(() => expect(props.onPaste).toHaveBeenCalledWith("git status\n"));
    expect(screen.queryByTestId("terminal-paste-box")).toBeNull();
  });

  it("says so when the clipboard has no text", async () => {
    readText.mockResolvedValue("");
    const props = renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    expect(await screen.findByText("The clipboard has no text to paste.")).toBeTruthy();
    expect(props.onPaste).not.toHaveBeenCalled();
  });

  it.each([
    ["refused", () => readText.mockRejectedValue(new DOMException("denied", "NotAllowedError"))],
    ["unavailable", () => Reflect.deleteProperty(navigator, "clipboard")],
  ])("offers a paste box when clipboard reads are %s", async (_name, arrange) => {
    arrange();
    const props = renderBar();

    fireEvent.click(screen.getByRole("button", { name: "Paste" }));

    const box = await screen.findByTestId("terminal-paste-box");
    const send = within(box).getByRole("button", { name: "Send to terminal" });
    expect((send as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(box).getByLabelText("Text to paste"), {
      target: { value: "make test" },
    });
    fireEvent.click(send);

    expect(props.onPaste).toHaveBeenCalledWith("make test");
    await waitFor(() => expect(screen.queryByTestId("terminal-paste-box")).toBeNull());
  });

  it("disables Paste while disconnected and hides it for read-only terminals", () => {
    renderBar({ pasteEnabled: false });
    expect((screen.getByRole("button", { name: "Paste" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    cleanup();

    renderBar({ pasteAvailable: false });
    expect(screen.queryByRole("button", { name: "Paste" })).toBeNull();
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
  });
});
