import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { and, equals, setUserKeybindingRule } from "@/actions";
import { resetKeybindingStoreForTesting } from "@/actions/KeybindingStore";
import { useFormattedActionKeybinding } from "./useFormattedActionKeybinding";

function composerContext(submitWithModEnter: boolean) {
  return and(
    equals("composerSuggestionsOpen", false),
    equals("composerEnterInserts", false),
    equals("composerSubmitWithModEnter", submitWithModEnter),
  );
}

beforeEach(() => {
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

describe("useFormattedActionKeybinding", () => {
  it("selects the binding active for the supplied context", () => {
    const plain = renderHook(() =>
      useFormattedActionKeybinding("composer.action.send", {
        mode: "composer",
        context: composerContext(false),
      }),
    );
    expect(plain.result.current).toBe("↵");
    plain.unmount();

    const modified = renderHook(() =>
      useFormattedActionKeybinding("composer.action.send", {
        mode: "composer",
        context: composerContext(true),
      }),
    );
    expect(modified.result.current).toBe("Ctrl+↵");
  });

  it("updates a contextual hint after a user rebind", () => {
    const hook = renderHook(() =>
      useFormattedActionKeybinding("composer.action.send", {
        mode: "composer",
        context: composerContext(true),
      }),
    );
    act(() => {
      expect(
        setUserKeybindingRule({
          id: "composer.send.primaryEnter",
          action: "composer.action.send",
          sequence: "ctrl+shift+j",
          mode: "composer",
        }),
      ).toEqual({ ok: true, changed: true });
    });
    expect(hook.result.current).toBe("Ctrl+Shift+J");
  });
});
