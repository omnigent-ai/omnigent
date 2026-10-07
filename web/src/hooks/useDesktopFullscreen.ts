import { useEffect, useState } from "react";

import {
  getDesktopFullScreen,
  isMacElectronShell,
  onDesktopFullScreenChanged,
} from "@/lib/nativeBridge";

/** True while the surrounding desktop shell's window is native-fullscreen. */
export function useDesktopFullscreen(): boolean {
  const [fullscreen, setFullscreen] = useState(false);
  useEffect(() => {
    // Only the macOS desktop shell has a native-fullscreen signal to track.
    if (!isMacElectronShell()) return;
    let disposed = false;
    let sawTransition = false;
    const unsubscribe = onDesktopFullScreenChanged((value) => {
      sawTransition = true;
      setFullscreen(value);
    });
    // A transition that lands during the IPC round trip is newer than the read.
    void getDesktopFullScreen().then((value) => {
      if (!disposed && !sawTransition) setFullscreen(value);
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, []);
  return fullscreen;
}
