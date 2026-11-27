import { createRef } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setEmbedRoot } from "@/lib/host";
import { SelectionPopup, SelectionPopupActions } from "./SelectionPopup";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  window.getSelection()?.removeAllRanges();
});

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

  const range = document.createRange();
  range.selectNodeContents(containerRef.current!);
  range.getBoundingClientRect = () => new DOMRect(10, 30, 80, 20);
  window.getSelection()!.addRange(range);
  fireEvent(document, new Event("selectionchange"));

  const reply = screen.getByRole("button", { name: "Reply ↵" });
  expect(reply.parentElement?.parentElement).toBe(embedded ? container : document.body);
  expect(reply.parentElement).toHaveStyle({ position: "fixed", left: "50px", top: "30px" });
  fireEvent.click(reply);
  expect(onReply).toHaveBeenCalledExactlyOnceWith("Selected message");
  expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
});

it("shows the actions above a given point for a selection tracked elsewhere", () => {
  const onReply = vi.fn();
  const onAskInSideChat = vi.fn();
  const onAddComment = vi.fn();
  render(
    <SelectionPopupActions
      position={{ x: 120, y: 80 }}
      onReply={onReply}
      onAskInSideChat={onAskInSideChat}
      onAddComment={onAddComment}
    />,
  );

  const reply = screen.getByRole("button", { name: "Reply ↵" });
  expect(reply.parentElement).toHaveStyle({ position: "fixed", left: "120px", top: "80px" });
  fireEvent.click(reply);
  fireEvent.click(screen.getByRole("button", { name: "Ask in side chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Add comment" }));
  expect(onReply).toHaveBeenCalledTimes(1);
  expect(onAskInSideChat).toHaveBeenCalledTimes(1);
  expect(onAddComment).toHaveBeenCalledTimes(1);
});

it("offers only Reply without side chat or comment handlers", () => {
  render(<SelectionPopupActions position={{ x: 0, y: 0 }} onReply={() => undefined} />);
  expect(screen.queryByRole("button", { name: "Ask in side chat" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
});
