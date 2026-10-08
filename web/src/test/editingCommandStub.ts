import { vi } from "vitest";

/**
 * jsdom lacks ``document.execCommand``; this stand-in applies ``delete`` /
 * ``insertText`` to the focused field like a browser (in-place edit plus an
 * ``input`` event, no ``value`` setter). Remove via removeEditingCommandStub.
 */
let priorDescriptor: PropertyDescriptor | undefined;

export function installEditingCommandStub() {
  priorDescriptor = Object.getOwnPropertyDescriptor(document, "execCommand");
  const stub = vi.fn((command: string, _showUi?: boolean, value?: string): boolean => {
    const field = document.activeElement;
    if (!(field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement)) return false;
    if (command !== "delete" && command !== "insertText") return false;
    const text = command === "delete" ? "" : (value ?? "");
    field.setRangeText(text, field.selectionStart ?? 0, field.selectionEnd ?? 0, "end");
    field.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        inputType: command === "delete" ? "deleteContentBackward" : "insertText",
        data: text || null,
      }),
    );
    return true;
  });
  Object.defineProperty(document, "execCommand", {
    configurable: true,
    writable: true,
    value: stub,
  });
  return stub;
}

export function removeEditingCommandStub(): void {
  if (priorDescriptor) {
    Object.defineProperty(document, "execCommand", priorDescriptor);
    priorDescriptor = undefined;
  } else {
    delete (document as { execCommand?: unknown }).execCommand;
  }
}
