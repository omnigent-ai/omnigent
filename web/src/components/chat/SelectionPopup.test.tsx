import { createRef } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { SelectionPopup } from "./SelectionPopup";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  window.getSelection()?.removeAllRanges();
});

function selectText(node: Node, notify = true) {
  const range = document.createRange();
  range.selectNodeContents(node);
  range.getBoundingClientRect = () => new DOMRect(10, 30, 80, 20);
  window.getSelection()!.removeAllRanges();
  window.getSelection()!.addRange(range);
  if (notify) fireEvent(document, new Event("selectionchange"));
  return range;
}

function renderPopup(onAskInSideChat?: (text: string) => void) {
  const containerRef = createRef<HTMLDivElement>();
  const onReply = vi.fn();
  const result = render(
    <>
      <div ref={containerRef}>Selected message</div>
      <div data-testid="outside">Outside text</div>
      <SelectionPopup
        containerRef={containerRef}
        onReply={onReply}
        onAskInSideChat={onAskInSideChat}
      />
    </>,
  );
  selectText(containerRef.current!);
  return { ...result, containerRef, onReply };
}

it.each([true, false])("portals outside the transcript (embedded: %s)", (embedded) => {
  const containerRef = createRef<HTMLDivElement>();
  const onReply = vi.fn();
  const { container } = render(
    <>
      <div ref={containerRef}>Selected message</div>
      <SelectionPopup containerRef={containerRef} onReply={onReply} />
    </>,
  );
  if (embedded) setEmbedRoot(container);

  selectText(containerRef.current!);

  const reply = screen.getByRole("button", { name: "Reply ↵" });
  expect(reply.parentElement?.parentElement).toBe(embedded ? container : document.body);
  expect(reply.parentElement).toHaveStyle({ position: "fixed", left: "50px", top: "30px" });
  fireEvent.click(reply);
  expect(onReply).toHaveBeenCalledExactlyOnceWith("Selected message");
  expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
});

it.each([false, true])("acts on selected text with Enter (Alt: %s)", (altKey) => {
  const onAskInSideChat = vi.fn();
  const { onReply } = renderPopup(onAskInSideChat);

  expect(screen.getByRole("button", { name: "Reply ↵" })).toHaveAttribute(
    "aria-keyshortcuts",
    "Enter",
  );
  expect(screen.getByRole("button", { name: "Ask in side chat" })).toHaveAttribute(
    "aria-keyshortcuts",
    "Alt+Enter",
  );
  expect(fireEvent.keyDown(document.body, { key: "Enter", altKey })).toBe(false);
  expect(altKey ? onAskInSideChat : onReply).toHaveBeenCalledExactlyOnceWith("Selected message");
  expect(altKey ? onReply : onAskInSideChat).not.toHaveBeenCalled();
  expect(window.getSelection()!.isCollapsed).toBe(true);
  expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
});

it("leaves Alt+Enter alone when side chat is unavailable", () => {
  const { onReply } = renderPopup();
  expect(screen.queryByRole("button", { name: "Ask in side chat" })).toBeNull();
  expect(fireEvent.keyDown(document.body, { key: "Enter", altKey: true })).toBe(true);
  expect(onReply).not.toHaveBeenCalled();
  expect(window.getSelection()!.toString()).toBe("Selected message");
});

it("uses the live selection even before selectionchange is delivered", () => {
  const { containerRef, onReply } = renderPopup();
  const range = selectText(containerRef.current!, false);
  range.setStart(containerRef.current!.firstChild!, 9);
  fireEvent.keyDown(document.body, { key: "Enter" });
  expect(onReply).toHaveBeenCalledExactlyOnceWith("message");
});

it.each(["outside", "cross-boundary", "collapsed", "cleared", "whitespace"])(
  "ignores a stale popup after the selection becomes %s",
  (selectionKind) => {
    const onAskInSideChat = vi.fn();
    const { containerRef, onReply } = renderPopup(onAskInSideChat);
    const selection = window.getSelection()!;
    if (selectionKind === "cleared") {
      selection.removeAllRanges();
    } else if (selectionKind === "collapsed") {
      selection.collapseToStart();
    } else if (selectionKind === "whitespace") {
      containerRef.current!.textContent = "   ";
      selectText(containerRef.current!, false);
    } else if (selectionKind === "outside") {
      selectText(screen.getByTestId("outside"), false);
    } else {
      selection.getRangeAt(0).setEnd(screen.getByTestId("outside").firstChild!, 7);
    }
    expect(fireEvent.keyDown(document.body, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(document.body, { key: "Enter", altKey: true })).toBe(true);
    expect(onReply).not.toHaveBeenCalled();
    expect(onAskInSideChat).not.toHaveBeenCalled();
  },
);

it.each([
  { repeat: true },
  { isComposing: true },
  { ctrlKey: true },
  { metaKey: true },
  { shiftKey: true },
  { altKey: true, shiftKey: true },
])("ignores Enter with %j", (modifiers) => {
  const onAskInSideChat = vi.fn();
  const { onReply } = renderPopup(onAskInSideChat);
  expect(fireEvent.keyDown(document.body, { key: "Enter", ...modifiers })).toBe(true);
  expect(onReply).not.toHaveBeenCalled();
  expect(onAskInSideChat).not.toHaveBeenCalled();
});

it.each(["textarea", "input", "contenteditable", "button", "monaco-editor", "xterm"])(
  "preserves Enter in a focused %s while transcript text is still selected",
  (surface) => {
    const onAskInSideChat = vi.fn();
    const { container, onReply } = renderPopup(onAskInSideChat);
    const target = document.createElement(
      ["textarea", "input", "button"].includes(surface) ? surface : "div",
    );
    target.tabIndex = 0;
    if (surface === "contenteditable") target.setAttribute("contenteditable", "true");
    else target.className = surface;
    container.append(target);
    target.focus();
    expect(fireEvent.keyDown(target, { key: "Enter" })).toBe(true);
    expect(fireEvent.keyDown(target, { key: "Enter", altKey: true })).toBe(true);
    expect(onReply).not.toHaveBeenCalled();
    expect(onAskInSideChat).not.toHaveBeenCalled();
  },
);

it("leaves modal and already-handled keys alone", () => {
  const { container, onReply } = renderPopup();
  const modal = document.createElement("div");
  modal.setAttribute("aria-modal", "true");
  container.append(modal);
  expect(fireEvent.keyDown(document.body, { key: "Enter" })).toBe(true);
  modal.remove();

  const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  event.preventDefault();
  fireEvent(document.body, event);
  expect(onReply).not.toHaveBeenCalled();
});

it("keeps Enter activation of the focused side-chat button", () => {
  const onAskInSideChat = vi.fn();
  const { onReply } = renderPopup(onAskInSideChat);
  const button = screen.getByRole("button", { name: "Ask in side chat" });
  button.focus();
  expect(fireEvent.keyDown(button, { key: "Enter" })).toBe(true);
  fireEvent.click(button);
  expect(onAskInSideChat).toHaveBeenCalledExactlyOnceWith("Selected message");
  expect(onReply).not.toHaveBeenCalled();
});

it("removes the shortcut listener when unmounted", () => {
  const { unmount, onReply } = renderPopup();
  unmount();
  expect(fireEvent.keyDown(document.body, { key: "Enter" })).toBe(true);
  expect(onReply).not.toHaveBeenCalled();
});

it("routes the shortcut only to the transcript containing the selection", () => {
  const first = renderPopup();
  const second = renderPopup();
  fireEvent.keyDown(document.body, { key: "Enter" });
  expect(first.onReply).not.toHaveBeenCalled();
  expect(second.onReply).toHaveBeenCalledExactlyOnceWith("Selected message");
});
