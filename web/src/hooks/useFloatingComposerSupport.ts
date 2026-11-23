import { useEffect, useRef } from "react";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import {
  floatingComposerBridge,
  getDesktopFeatures,
  type FloatingComposerFirstMessage,
} from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";
import { setPendingInitialPrompt } from "@/store/chatStore";

/** The panel version this bundle renders at `/compose/floating`; the shell accepts only it. */
const FLOATING_COMPOSER_VERSION = 1;

const SESSION_PATH = /^\/c\/([A-Za-z0-9_-]{1,128})$/;

/**
 * Tells the desktop shell that this main window's page can render the floating New Agent
 * panel, so ⇧⌘Space opens it, when `floating_composer` is on and the shell supports it.
 * The shell forgets this when the page is replaced, so each document announces again.
 * Sessions the panel launches open here, and this page sends their first message.
 */
export function useFloatingComposerSupport(): void {
  const enabled = isFeatureEnabled(useServerInfo(), "floating_composer");
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  useEffect(() => {
    const bridge = floatingComposerBridge();
    if (!enabled || !bridge) return;
    let cancelled = false;
    let stopListening: (() => void) | undefined;
    void getDesktopFeatures()
      .then((features) => {
        if (cancelled || features?.floatingComposer !== true) return;
        bridge.announceSupport(FLOATING_COMPOSER_VERSION);
        stopListening = bridge.onLaunch?.((launch) =>
          sendLaunchedFirstMessage(launch, navigateRef.current),
        );
      })
      // The bridge call can throw while the page or its preload is going away.
      .catch((err: unknown) => console.warn("[floatingComposer] couldn't announce support:", err));
    return () => {
      cancelled = true;
      stopListening?.();
    };
  }, [enabled]);
}

/** Open the launched session with its first message, as this window's own New Chat does. */
function sendLaunchedFirstMessage(
  { path, firstMessage }: { path: string; firstMessage: FloatingComposerFirstMessage },
  navigate: (to: string) => void,
): void {
  const sessionId = SESSION_PATH.exec(path)?.[1];
  if (sessionId === undefined) return;
  setPendingInitialPrompt(sessionId, {
    text: firstMessage.text,
    skill: firstMessage.skill,
    files: firstMessage.files.map(
      ({ data, name, type, lastModified }) => new File([data], name, { type, lastModified }),
    ),
  });
  navigate(path);
}
