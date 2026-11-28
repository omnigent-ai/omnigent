import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { rovingFocusTarget, useRovingFocus } from "./useRovingFocus";

describe("rovingFocusTarget", () => {
  it("steps with the arrows and stops at the ends", () => {
    expect(rovingFocusTarget("ArrowDown", 0, 3)).toBe(1);
    expect(rovingFocusTarget("ArrowDown", 2, 3)).toBe(2);
    expect(rovingFocusTarget("ArrowUp", 1, 3)).toBe(0);
    expect(rovingFocusTarget("ArrowUp", 0, 3)).toBe(0);
  });

  it("jumps to the ends with Home and End", () => {
    expect(rovingFocusTarget("Home", 2, 3)).toBe(0);
    expect(rovingFocusTarget("End", 0, 3)).toBe(2);
  });

  it("ignores other keys and empty lists", () => {
    expect(rovingFocusTarget("Enter", 0, 3)).toBeNull();
    expect(rovingFocusTarget("ArrowLeft", 0, 3)).toBeNull();
    expect(rovingFocusTarget("ArrowDown", 0, 0)).toBeNull();
  });
});

function List({
  count,
  initialIndex,
  focusRequest,
  onSettled,
}: {
  count: number;
  initialIndex?: number;
  focusRequest?: object | null;
  onSettled?: () => void;
}) {
  const { itemProps } = useRovingFocus(count, {
    initialIndex,
    focusRequest,
    onFocusRequestSettled: onSettled,
  });
  return (
    <ul>
      {Array.from({ length: count }, (_, index) => (
        <li key={index}>
          <button type="button" {...itemProps(index)}>
            Item {index}
          </button>
        </li>
      ))}
    </ul>
  );
}

const item = (index: number) => screen.getByRole("button", { name: `Item ${index}` });
const tabStops = () => screen.getAllByRole("button").filter((button) => button.tabIndex === 0);

describe("useRovingFocus", () => {
  it("keeps one tab stop, which follows focus as the keys move it", () => {
    render(<List count={3} />);
    expect(tabStops()).toEqual([item(0)]);
    item(0).focus();
    fireEvent.keyDown(item(0), { key: "ArrowDown" });
    expect(item(1)).toHaveFocus();
    expect(tabStops()).toEqual([item(1)]);
    fireEvent.keyDown(item(1), { key: "End" });
    expect(item(2)).toHaveFocus();
    fireEvent.keyDown(item(2), { key: "Home" });
    expect(item(0)).toHaveFocus();
  });

  it("leaves keys with modifiers to others, such as the app's shortcuts", () => {
    render(<List count={3} />);
    item(0).focus();
    const notPrevented = fireEvent.keyDown(item(0), { key: "ArrowDown", metaKey: true });
    expect(notPrevented).toBe(true);
    expect(item(0)).toHaveFocus();
  });

  it("starts the tab stop on the initial item, and focuses it on request, once", () => {
    const onSettled = vi.fn();
    const { rerender } = render(<List count={3} initialIndex={2} onSettled={onSettled} />);
    expect(tabStops()).toEqual([item(2)]);
    expect(document.body).toHaveFocus();
    const request = {};
    rerender(<List count={3} initialIndex={2} focusRequest={request} onSettled={onSettled} />);
    expect(item(2)).toHaveFocus();
    expect(onSettled).toHaveBeenCalledOnce();
  });

  it("carries out a request pending when the list mounts, as after its rows load", () => {
    const onSettled = vi.fn();
    render(<List count={2} initialIndex={1} focusRequest={{}} onSettled={onSettled} />);
    expect(item(1)).toHaveFocus();
    expect(onSettled).toHaveBeenCalledOnce();
  });

  it("keeps the tab stop on the list when it shrinks under it", () => {
    const { rerender } = render(<List count={3} initialIndex={2} />);
    rerender(<List count={2} initialIndex={2} />);
    expect(tabStops()).toEqual([item(1)]);
  });
});
