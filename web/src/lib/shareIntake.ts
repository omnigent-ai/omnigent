import { useEffect } from "react";
import { useChatStore } from "@/store/chatStore";

/** Matches an OS-share hand-off URL fragment, e.g. `#shared-text=hello%20world`. */
const SHARE_TEXT_PATTERN = /^#shared-text=(.*)$/;

/**
 * One-time bootstrap for content handed off by an OS Share action (see the
 * `dev.ozzieba.geckowebapps.ShareActivity` Android intake, which forwards
 * shared text as a `#shared-text=` URL fragment rather than a query
 * parameter, so it never reaches a server access log). Runs once per page
 * load: reads the fragment, queues it into {@link ChatState.pendingComposerText}
 * (drained by the composer in `ChatPage.tsx`), then strips it from the URL so
 * a refresh or history navigation doesn't re-apply it.
 *
 * Deliberately hash-based, not a query parameter: this repo's routes are
 * path-based (`react-router` `BrowserRouter`), so the hash is otherwise
 * unused here and can't collide with route matching.
 */
export function useShareIntake(): void {
  useEffect(() => {
    const match = SHARE_TEXT_PATTERN.exec(window.location.hash);
    if (match === null) return;
    let text: string;
    try {
      text = decodeURIComponent(match[1]);
    } catch {
      // Malformed percent-encoding: drop it rather than queue garbage text.
      text = "";
    }
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    if (text !== "") {
      useChatStore.getState().setPendingComposerText(text);
    }
    // Runs once at boot only: a share hand-off is a one-shot URL, not
    // something to re-check on every navigation.
  }, []);
}
