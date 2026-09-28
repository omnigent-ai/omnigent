import { useEffect, useState } from "react";
import {
  getIOSKeyboardViewportHeight,
  isIOSShell,
  onNativeKeyboardViewportChanged,
} from "@/lib/nativeBridge";

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

      setInset(getIOSNativeKeyboardInset());
    };

    const unsubscribe = onNativeKeyboardViewportChanged(sync);
    sync();
    window.visualViewport?.addEventListener("resize", sync);
    window.visualViewport?.addEventListener("scroll", sync);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    window.addEventListener("focusin", sync, true);
    window.addEventListener("focusout", sync, true);

    return () => {
      unsubscribe();
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

function getIOSNativeKeyboardInset(): number {
  const viewport = window.visualViewport;
  if (!viewport) return 0;

  // Fixed overlays sit outside the resized app shell. Reserve only the docked
  // keyboard's footprint, using WebKit's viewport on older native shells.
  const layoutBottom = window.innerHeight;
  const nativeHeight = getIOSKeyboardViewportHeight();
  const visibleBottom = nativeHeight ?? viewport.offsetTop + viewport.height;
  const inset = Math.max(0, Math.round(layoutBottom - visibleBottom));
  return nativeHeight !== null || inset > KEYBOARD_INSET_THRESHOLD_PX ? inset : 0;
}
