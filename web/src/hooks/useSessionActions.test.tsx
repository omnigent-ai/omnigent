import type { ReactNode } from "react";
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider, KeybindingDispatcher, useActions } from "@/actions";
import { sessionTarget, useSessionActions } from "./useSessionActions";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

const nativeShell = vi.fn(() => true);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => nativeShell(),
  isElectronShell: () => false,
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <ActionsProvider>
      <KeybindingDispatcher />
      {children}
    </ActionsProvider>
  );
}

function press(
  key: string,
  init: KeyboardEventInit = { ctrlKey: true },
  target: HTMLElement = document.body,
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ...init,
  });
  target.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  navigate.mockClear();
  nativeShell.mockReturnValue(true);
});
afterEach(cleanup);

describe("sessionTarget", () => {
  const ids = ["a", "b", "c"];

  it("steps and wraps in both directions", () => {
    expect(sessionTarget(ids, "b", 1)).toBe("c");
    expect(sessionTarget(ids, "b", -1)).toBe("a");
    expect(sessionTarget(ids, "c", 1)).toBe("a");
    expect(sessionTarget(ids, "a", -1)).toBe("c");
  });

  it("enters from the appropriate edge when off-list", () => {
    expect(sessionTarget(ids, undefined, 1)).toBe("a");
    expect(sessionTarget(ids, undefined, -1)).toBe("c");
    expect(sessionTarget([], undefined, 1)).toBeUndefined();
  });
});

describe("useSessionActions", () => {
  const ids = ["a", "b", "c"];

  it("navigates previous and next sessions from editable fields", () => {
    renderHook(() => useSessionActions(ids, ids, "b"), { wrapper });
    const input = document.createElement("input");
    document.body.append(input);
    press("ArrowDown", { ctrlKey: true }, input);
    expect(navigate).toHaveBeenLastCalledWith("/c/c");
    press("ArrowUp", { metaKey: true }, input);
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
    input.remove();
  });

  it("yields session navigation to a widget that prevented the chord", () => {
    renderHook(() => useSessionActions(ids, ids, "a"), { wrapper });
    const widget = document.createElement("div");
    widget.addEventListener("keydown", (event) => event.preventDefault());
    document.body.append(widget);
    press("ArrowDown", { ctrlKey: true }, widget);
    expect(navigate).not.toHaveBeenCalled();
    widget.remove();
  });

  it("ignores bare, Alt, and Shift session arrows", () => {
    renderHook(() => useSessionActions(ids, ids, "a"), { wrapper });
    press("ArrowDown", {});
    press("ArrowDown", { ctrlKey: true, altKey: true });
    press("ArrowDown", { ctrlKey: true, shiftKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves empty and already-active single-session navigation alone", () => {
    const hook = renderHook(({ ordered, active }) => useSessionActions(ordered, ordered, active), {
      wrapper,
      initialProps: { ordered: [] as string[], active: "a" as string | undefined },
    });
    const empty = press("ArrowDown");
    expect(empty.defaultPrevented).toBe(false);
    hook.rerender({ ordered: ["a"], active: "a" });
    const only = press("ArrowDown");
    expect(navigate).not.toHaveBeenCalled();
    expect(only.defaultPrevented).toBe(true);
  });

  it("executes a pinned slot directly by typed action args", () => {
    const { result } = renderHook(
      () => {
        useSessionActions(ids, ids, undefined);
        return useActions();
      },
      { wrapper },
    );
    result.current.execute({
      action: "session.action.openPinned",
      args: { slot: 2 },
      source: "api",
    });
    expect(navigate).toHaveBeenCalledWith("/c/c");
  });

  it("opens native pinned slots 1 through 0 and consumes active slots", () => {
    const ten = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    const hook = renderHook(({ active }) => useSessionActions(ten, ten, active), {
      wrapper,
      initialProps: { active: undefined as string | undefined },
    });
    press("1");
    expect(navigate).toHaveBeenLastCalledWith("/c/a");
    press("0");
    expect(navigate).toHaveBeenLastCalledWith("/c/j");
    hook.rerender({ active: "a" });
    const active = press("1");
    expect(active.defaultPrevented).toBe(true);
  });

  it("fires pinned slots from focused text fields", () => {
    renderHook(() => useSessionActions(ids, ids, undefined), { wrapper });
    const input = document.createElement("input");
    document.body.append(input);
    press("2", { ctrlKey: true }, input);
    expect(navigate).toHaveBeenCalledWith("/c/b");
    input.remove();
  });

  it("leaves empty or unassigned pinned slots untouched", () => {
    const hook = renderHook(({ pinned }) => useSessionActions(ids, pinned, undefined), {
      wrapper,
      initialProps: { pinned: [] as string[] },
    });
    const empty = press("1");
    expect(empty.defaultPrevented).toBe(false);
    hook.rerender({ pinned: ids });
    const event = press("5");
    expect(navigate).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("uses physical primary+Alt+digit in browsers and rejects AltGraph", () => {
    nativeShell.mockReturnValue(false);
    renderHook(() => useSessionActions(ids, ids, undefined), { wrapper });
    const plain = press("1", { ctrlKey: true, code: "Digit1" });
    expect(plain.defaultPrevented).toBe(false);
    const modified = press("¡", { metaKey: true, altKey: true, code: "Digit1" });
    expect(modified.defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenLastCalledWith("/c/a");

    navigate.mockClear();
    const altGraph = new KeyboardEvent("keydown", {
      key: "²",
      code: "Digit2",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    altGraph.getModifierState = (key) => key === "AltGraph";
    document.body.dispatchEvent(altGraph);
    expect(navigate).not.toHaveBeenCalled();
    expect(altGraph.defaultPrevented).toBe(false);
  });
});
