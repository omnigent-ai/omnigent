import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ComposerConfigChoice } from "@/components/composer/ComposerConfigSections";
import { EmbeddedProvider } from "@/lib/embedded";
import { setEmbedRoot } from "@/lib/host";
import { useComposerConfigCycleHotkeys } from "./useComposerConfigCycleHotkeys";

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function event(init: KeyboardEventInit = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    code: "KeyM",
    ctrlKey: true,
    altKey: true,
    ...init,
  });
}

function choice(key: string, overrides: Partial<ComposerConfigChoice> = {}): ComposerConfigChoice {
  return { key, label: key, checked: false, ...overrides };
}

describe("useComposerConfigCycleHotkeys", () => {
  it("cycles menu callbacks in order, skipping unavailable choices and wrapping", () => {
    const select = vi.fn();
    const models = [
      choice("first", { checked: true, onSelect: () => select("first") }),
      choice("disabled", { disabled: true, onSelect: () => select("disabled") }),
      choice("static"),
      choice("last", { onSelect: () => select("last") }),
    ];
    const efforts = [choice("low", { onSelect: () => select("low") })];
    const { rerender } = renderHook(
      ({ models: currentModels }) => useComposerConfigCycleHotkeys(currentModels, efforts),
      { initialProps: { models } },
    );

    const next = event({ key: "µ" });
    act(() => window.dispatchEvent(next));
    expect(next.defaultPrevented).toBe(true);
    expect(select.mock.calls).toEqual([["last"]]);

    rerender({ models: models.map((option) => ({ ...option, checked: option.key === "last" })) });
    act(() => window.dispatchEvent(event()));
    act(() => window.dispatchEvent(event({ code: "KeyE" })));
    expect(select.mock.calls).toEqual([["last"], ["first"], ["low"]]);
  });

  it.each([
    undefined,
    [],
    [choice("selected", { checked: true })],
    [choice("disabled", { disabled: true })],
    [choice("static")],
  ])("leaves the key alone when no change is available (%j)", (options) => {
    const select = vi.fn();
    const models = options?.map((option) =>
      option.key === "static" ? option : { ...option, onSelect: select },
    );
    renderHook(() => useComposerConfigCycleHotkeys(models, undefined));
    const pressed = event();
    window.dispatchEvent(pressed);
    expect(pressed.defaultPrevented).toBe(false);
    expect(select).not.toHaveBeenCalled();
  });

  it("starts an unknown selection at the first selectable choice", () => {
    const select = vi.fn();
    renderHook(() =>
      useComposerConfigCycleHotkeys(
        [
          choice("disabled", { disabled: true, onSelect: () => select("disabled") }),
          choice("first", { onSelect: () => select("first") }),
          choice("last", { onSelect: () => select("last") }),
        ],
        undefined,
      ),
    );
    act(() => window.dispatchEvent(event()));
    expect(select).toHaveBeenCalledExactlyOnceWith("first");
  });

  it("ignores other modifiers, held keys, IME, AltGraph, and previously handled events", () => {
    const select = vi.fn();
    renderHook(() =>
      useComposerConfigCycleHotkeys([choice("next", { onSelect: select })], undefined),
    );
    for (const init of [
      { ctrlKey: false },
      { altKey: false },
      { metaKey: true },
      { shiftKey: true },
      { code: "KeyL" },
      { repeat: true },
      { isComposing: true },
      { keyCode: 229 },
    ]) {
      const pressed = event(init);
      window.dispatchEvent(pressed);
      expect(pressed.defaultPrevented).toBe(false);
    }
    const altGraph = event();
    vi.spyOn(altGraph, "getModifierState").mockImplementation((key) => key === "AltGraph");
    window.dispatchEvent(altGraph);
    expect(altGraph.defaultPrevented).toBe(false);
    const handled = event();
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(select).not.toHaveBeenCalled();
  });

  it.each(["xterm", "monaco-editor", "dialog", "alertdialog"])(
    "leaves focused %s controls alone",
    (surface) => {
      const select = vi.fn();
      renderHook(() =>
        useComposerConfigCycleHotkeys([choice("next", { onSelect: select })], undefined),
      );
      const owner = document.createElement("div");
      if (surface === "xterm" || surface === "monaco-editor") owner.className = surface;
      else owner.setAttribute("role", surface);
      const input = document.createElement("input");
      owner.appendChild(input);
      document.body.appendChild(owner);
      input.focus();
      const pressed = event();
      input.dispatchEvent(pressed);
      expect(pressed.defaultPrevented).toBe(false);
      expect(select).not.toHaveBeenCalled();
    },
  );

  it("only claims embedded shortcuts from inside the app", () => {
    const select = vi.fn();
    const root = document.createElement("div");
    const composer = document.createElement("textarea");
    root.appendChild(composer);
    document.body.appendChild(root);
    setEmbedRoot(root);
    renderHook(
      () => useComposerConfigCycleHotkeys([choice("next", { onSelect: select })], undefined),
      { wrapper: EmbeddedProvider },
    );

    const outside = event();
    document.body.dispatchEvent(outside);
    expect(outside.defaultPrevented).toBe(false);
    expect(select).not.toHaveBeenCalled();

    const inside = event();
    act(() => composer.dispatchEvent(inside));
    expect(inside.defaultPrevented).toBe(true);
    expect(select).toHaveBeenCalledOnce();
  });

  it("unbinds when disabled and after unmount", () => {
    const select = vi.fn();
    const { rerender, unmount } = renderHook(
      ({ enabled }) =>
        useComposerConfigCycleHotkeys([choice("next", { onSelect: select })], undefined, enabled),
      { initialProps: { enabled: true } },
    );
    rerender({ enabled: false });
    const disabled = event();
    window.dispatchEvent(disabled);
    expect(disabled.defaultPrevented).toBe(false);
    rerender({ enabled: true });
    unmount();
    const afterUnmount = event();
    window.dispatchEvent(afterUnmount);
    expect(afterUnmount.defaultPrevented).toBe(false);
    expect(select).not.toHaveBeenCalled();
  });

  it("keeps a preview for two seconds after cycling or a confirmed selection change", () => {
    vi.useFakeTimers();
    const select = vi.fn();
    const { result, rerender, unmount } = renderHook(
      ({ selected, pending }) =>
        useComposerConfigCycleHotkeys(
          ["first", "last"].map((key) =>
            choice(key, { checked: key === selected, onSelect: select }),
          ),
          [choice("low", { onSelect: select })],
          true,
          pending,
        ),
      { initialProps: { selected: "first", pending: false } },
    );
    expect(result.current.preview).toBeNull();
    act(() => window.dispatchEvent(event()));
    expect(result.current.preview).toBe("models");
    act(() => vi.advanceTimersByTime(1_500));
    act(() => window.dispatchEvent(event()));
    act(() => vi.advanceTimersByTime(1_500));
    expect(result.current.preview).toBe("models");
    rerender({ selected: "first", pending: true });
    act(() => vi.advanceTimersByTime(20_000));
    expect(result.current.preview).toBe("models");
    rerender({ selected: "last", pending: false });
    act(() => vi.advanceTimersByTime(1_500));
    expect(result.current.preview).toBe("models");
    // Recreated choices with the same selection do not extend the preview.
    rerender({ selected: "last", pending: false });
    act(() => vi.advanceTimersByTime(500));
    expect(result.current.preview).toBeNull();

    act(() => window.dispatchEvent(event({ code: "KeyE" })));
    expect(result.current.preview).toBe("efforts");
    act(() => result.current.dismissPreview());
    expect(result.current.preview).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    act(() => window.dispatchEvent(event()));
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("defers application until the last shortcut settles and cancels on dismissal", () => {
    vi.useFakeTimers();
    const apply = vi.fn();
    const cancel = vi.fn();
    const select = vi.fn();
    const { result, rerender } = renderHook(
      ({ drafted }) =>
        useComposerConfigCycleHotkeys(
          [choice("next", { onSelect: select })],
          [choice("high", { onSelect: select })],
          true,
          false,
          drafted ? { apply, cancel } : undefined,
        ),
      { initialProps: { drafted: true } },
    );
    act(() => window.dispatchEvent(event()));
    act(() => vi.advanceTimersByTime(1_500));
    act(() => window.dispatchEvent(event({ code: "KeyE" })));
    act(() => vi.advanceTimersByTime(1_999));
    expect(apply).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(apply).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
    rerender({ drafted: false });
    act(() => vi.advanceTimersByTime(2_000));
    expect(apply).toHaveBeenCalledOnce();

    rerender({ drafted: true });
    act(() => window.dispatchEvent(event()));
    act(() => result.current.dismissPreview());
    act(() => vi.advanceTimersByTime(3_000));
    expect(cancel).toHaveBeenCalledOnce();
    expect(apply).toHaveBeenCalledOnce();
    expect(result.current.preview).toBeNull();
  });

  it.each([
    { composerFocused: true, modifier: {} },
    { composerFocused: true, modifier: { ctrlKey: true } },
    { composerFocused: true, modifier: { metaKey: true } },
    { composerFocused: false, modifier: {} },
  ])("only consumes Enter to apply from a composer (%j)", ({ composerFocused, modifier }) => {
    const apply = vi.fn();
    const submit = vi.fn();
    renderHook(() =>
      useComposerConfigCycleHotkeys(
        [choice("next", { onSelect: vi.fn() })],
        undefined,
        true,
        false,
        { apply, cancel: vi.fn() },
      ),
    );
    const composer = document.createElement("div");
    if (composerFocused) composer.setAttribute("data-composer-card", "");
    const input = document.createElement("textarea");
    input.addEventListener("keydown", submit);
    composer.appendChild(input);
    document.body.appendChild(composer);
    input.focus();
    act(() => input.dispatchEvent(event()));
    submit.mockClear();
    const enter = event({
      code: "Enter",
      key: "Enter",
      ctrlKey: false,
      altKey: false,
      ...modifier,
    });
    act(() => input.dispatchEvent(enter));
    expect(enter.defaultPrevented).toBe(composerFocused);
    expect(apply).toHaveBeenCalledTimes(composerFocused ? 1 : 0);
    expect(submit).toHaveBeenCalledTimes(composerFocused ? 0 : 1);
  });
});
