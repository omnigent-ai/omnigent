// The native Close Tab accelerator also works while an embedded browser has focus.

import { useEffect, useLayoutEffect, useRef } from "react";

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
  useLayoutEffect(() => {
    latest.current = { closeSelectedTab, inSession, navigate };
  });

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
