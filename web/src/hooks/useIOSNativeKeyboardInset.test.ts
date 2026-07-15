import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useIOSNativeKeyboardVisible } from "./useIOSNativeKeyboardInset";

// The iOS shell hides the native Liquid Glass Chat/Terminal bar whenever an
// editable element is focused (it reads that as the on-screen keyboard opening).
// `copyText`'s execCommand fallback briefly focuses a hidden textarea to run the
// copy — marked `data-clipboard-helper` so it must NOT count as editable focus,
// otherwise a copy would dismiss the bar for the rest of the session (WebKit
// doesn't reliably fire `focusout` when a focused node is removed).

function setIOS(on: boolean): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "ios" };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  }
}

afterEach(() => {
  cleanup();
  setIOS(false);
  document.body.innerHTML = "";
});

describe("useIOSNativeKeyboardVisible editable-focus detection", () => {
  it("does not report keyboard-visible when a clipboard-helper textarea is focused", () => {
    setIOS(true);
    const { result } = renderHook(() => useIOSNativeKeyboardVisible(true, true));
    expect(result.current).toBe(false);

    const helper = document.createElement("textarea");
    helper.setAttribute("data-clipboard-helper", "");
    document.body.appendChild(helper);
    act(() => {
      helper.focus();
      window.dispatchEvent(new Event("focusin"));
    });

    expect(result.current).toBe(false);
  });

  it("reports keyboard-visible when a real textarea is focused", () => {
    setIOS(true);
    const { result } = renderHook(() => useIOSNativeKeyboardVisible(true, true));

    const input = document.createElement("textarea");
    document.body.appendChild(input);
    act(() => {
      input.focus();
      window.dispatchEvent(new Event("focusin"));
    });

    expect(result.current).toBe(true);
  });
});
