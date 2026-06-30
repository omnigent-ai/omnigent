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

  // Keyboard height = the layout viewport (the full webview, kept keyboard-
  // independent by the native shell's `.ignoresSafeArea(.keyboard)`) minus the
  // visible visual viewport. Measured against `window.innerHeight`, NOT the
  // app-shell: useIOSViewportLock resizes the shell down to the visual viewport
  // when the keyboard opens, so an app-shell-relative measurement would always
  // read ~0. This value is for fixed, full-viewport overlays (e.g. the mobile
  // TerminalsPanel) that the shell-lock does not resize — flow content inside
  // the shell is handled by the lock and needs no manual padding.
  const layoutBottom = window.innerHeight;
  const visibleBottom = viewport.offsetTop + viewport.height;
  return Math.max(0, Math.round(layoutBottom - visibleBottom));
}

function isEditableElementFocused(): boolean {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return false;
  return active.matches('input, textarea, select, [contenteditable="true"]');
}
