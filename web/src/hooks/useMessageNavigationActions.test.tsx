import type { ReactNode } from "react";
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider, KeybindingDispatcher } from "@/actions";
import { useMessageNavigationActions } from "./useMessageNavigationActions";

function wrapper({ children }: { children: ReactNode }) {
  return (
    <ActionsProvider>
      <KeybindingDispatcher />
      {children}
    </ActionsProvider>
  );
}

function press(key: "ArrowUp" | "ArrowDown", target: HTMLElement = document.body) {
  const event = new KeyboardEvent("keydown", {
    key,
    ctrlKey: true,
    altKey: true,
    bubbles: true,
    cancelable: true,
  });
  target.dispatchEvent(event);
  return event;
}

afterEach(cleanup);

describe("useMessageNavigationActions", () => {
  it("routes modified arrows to the shared transcript cursor", () => {
    const goPrev = vi.fn();
    const goNext = vi.fn();
    renderHook(
      () => useMessageNavigationActions({ goPrev, goNext, canPrev: true, canNext: true }),
      { wrapper },
    );
    expect(press("ArrowUp").defaultPrevented).toBe(true);
    expect(goPrev).toHaveBeenCalledOnce();
    expect(press("ArrowDown").defaultPrevented).toBe(true);
    expect(goNext).toHaveBeenCalledOnce();
  });

  it("yields to a focused widget that prevents the chord", () => {
    const goPrev = vi.fn();
    renderHook(
      () => useMessageNavigationActions({ goPrev, goNext: vi.fn(), canPrev: true, canNext: true }),
      { wrapper },
    );
    const widget = document.createElement("div");
    widget.addEventListener("keydown", (event) => event.preventDefault());
    document.body.append(widget);
    press("ArrowUp", widget);
    expect(goPrev).not.toHaveBeenCalled();
    widget.remove();
  });

  it("ignores unmodified arrows and preserves legacy Shift-modified navigation", () => {
    const goNext = vi.fn();
    renderHook(
      () => useMessageNavigationActions({ goPrev: vi.fn(), goNext, canPrev: true, canNext: true }),
      { wrapper },
    );
    document.body.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }),
    );
    document.body.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "ArrowDown",
        ctrlKey: true,
        altKey: true,
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(goNext).toHaveBeenCalledOnce();
  });
});
