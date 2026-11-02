// The desktop shell's Close Tab (CmdOrCtrl+W) is a native menu accelerator, so
// it fires even while an embedded Browser view holds focus and never reaches
// the page as a keydown. The shell forwards it here once this page opts in
// (see `onCloseTabRequest`); in a plain browser tab nothing subscribes and
// CmdOrCtrl+W stays the browser's own.

import { useEffect, useRef } from "react";

import { closeNativeWindow, onCloseTabRequest } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

/**
 * Handle Close Tab: close the workspace rail's selected tab; with none, leave
 * the session for the new-session page; anywhere else, close the window.
 *
 * @param closeSelectedTab Close the rail's selected tab. Returns false when
 *   nothing closable is selected, including when the rail is hidden.
 * @param inSession Whether a conversation is on screen.
 */
export function useCloseTabShortcut(closeSelectedTab: () => boolean, inSession: boolean): void {
  const navigate = useNavigate();
  // Held in a ref so the one subscription always sees the latest rail state.
  const latest = useRef({ closeSelectedTab, inSession, navigate });
  latest.current = { closeSelectedTab, inSession, navigate };

  useEffect(
    () =>
      onCloseTabRequest(() => {
        const current = latest.current;
        if (current.closeSelectedTab()) return;
        if (current.inSession) current.navigate("/");
        else closeNativeWindow();
      }),
    [],
  );
}
