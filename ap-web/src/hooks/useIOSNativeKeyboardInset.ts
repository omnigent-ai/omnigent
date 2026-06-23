import { useEffect, useState } from "react";
import { isIOSShell } from "@/lib/nativeBridge";

const KEYBOARD_INSET_THRESHOLD_PX = 80;

export function useIOSNativeKeyboardInset(enabled = true): number {
  const [inset, setInset] = useState(0);

  useEffect(() => {
    if (!enabled || !isIOSShell()) {
      setInset(0);
      return;
    }

    const sync = () => {
      const viewport = window.visualViewport;
      if (!viewport) {
        setInset(0);
        return;
      }

      const nextInset = getIOSNativeKeyboardInset();
      setInset(nextInset > KEYBOARD_INSET_THRESHOLD_PX ? nextInset : 0);
    };

    sync();
    window.visualViewport?.addEventListener("resize", sync);
    window.visualViewport?.addEventListener("scroll", sync);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    window.addEventListener("focusin", sync, true);
    window.addEventListener("focusout", sync, true);

    return () => {
      window.visualViewport?.removeEventListener("resize", sync);
      window.visualViewport?.removeEventListener("scroll", sync);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
      window.removeEventListener("focusin", sync, true);
      window.removeEventListener("focusout", sync, true);
    };
  }, [enabled]);

  return inset;
}

export function useIOSNativeKeyboardVisible(enabled = true, includeEditableFocus = true): boolean {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (!enabled || !isIOSShell()) {
      setVisible(false);
      return;
    }

    const sync = () => {
      setVisible(
        getIOSNativeKeyboardInset() > KEYBOARD_INSET_THRESHOLD_PX ||
          (includeEditableFocus && isEditableElementFocused()),
      );
    };

    sync();
    window.visualViewport?.addEventListener("resize", sync);
    window.visualViewport?.addEventListener("scroll", sync);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    window.addEventListener("focusin", sync, true);
    window.addEventListener("focusout", sync, true);

    return () => {
      window.visualViewport?.removeEventListener("resize", sync);
      window.visualViewport?.removeEventListener("scroll", sync);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
      window.removeEventListener("focusin", sync, true);
      window.removeEventListener("focusout", sync, true);
    };
  }, [enabled, includeEditableFocus]);

  return visible;
}

function getIOSNativeKeyboardInset(): number {
  const viewport = window.visualViewport;
  if (!viewport) return 0;

  const shellBottom =
    document.querySelector<HTMLElement>("[data-ios-native].app-shell")?.getBoundingClientRect()
      .bottom ?? window.innerHeight;
  const visibleBottom = viewport.offsetTop + viewport.height;
  return Math.max(0, Math.round(shellBottom - visibleBottom));
}

function isEditableElementFocused(): boolean {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return false;
  return active.matches('input, textarea, select, [contenteditable="true"]');
}
