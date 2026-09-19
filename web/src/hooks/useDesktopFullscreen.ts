// Reactive "is the desktop window native-fullscreen?" hook.
//
// The Electron main process forwards enter/leave-full-screen through the
// preload bridge; the initial read covers a renderer that loads while the
// window is already fullscreen. Always false in a plain browser and on
// desktop shells that predate the bridge — they keep the windowed layout.

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
