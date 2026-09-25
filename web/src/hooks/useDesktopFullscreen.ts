import { useEffect, useState } from "react";

import { getDesktopFullScreen, onDesktopFullScreenChanged } from "@/lib/nativeBridge";

/** True while the surrounding desktop shell's window is native-fullscreen. */
export function useDesktopFullscreen(): boolean {
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    let disposed = false;
    void getDesktopFullScreen().then((value) => {
      if (!disposed) setFullscreen(value);
    });
    const unsubscribe = onDesktopFullScreenChanged(setFullscreen);
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, []);
  return fullscreen;
}
