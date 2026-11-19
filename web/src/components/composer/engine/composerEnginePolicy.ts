// Which engine the main chat composer runs. CodeMirror needs the release flag, a desktop
// viewport and a loaded chunk; mobile always keeps the textarea.

import { useEffect, useSyncExternalStore } from "react";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  subscribeCodeMirrorComposerLoad,
  type CodeMirrorComposerLoad,
} from "./codeMirrorComposerLoader";
import type { ComposerInputEngine } from "./composerInput";

/** How often a failed load may retry on its own, on window focus. */
export const COMPOSER_LOAD_RETRY_INTERVAL_MS = 60_000;

/** The engine to render: CodeMirror only with the flag on, off mobile, once its chunk is ready. */
export function effectiveComposerEngine(
  flagOn: boolean,
  mobile: boolean,
  load: CodeMirrorComposerLoad["status"],
): ComposerInputEngine {
  return flagOn && !mobile && load === "ready" ? "codemirror" : "textarea";
}

export function useCodeMirrorComposerLoad(): CodeMirrorComposerLoad {
  return useSyncExternalStore(subscribeCodeMirrorComposerLoad, codeMirrorComposerLoad);
}

/**
 * The engine the main chat composer should run now. Loads the CodeMirror chunk while the
 * flag is on and the viewport isn't mobile, and retries a failed load on window focus, at
 * most once a minute.
 */
export function useRequestedComposerEngine(): ComposerInputEngine {
  const flagOn = isFeatureEnabled(useServerInfo(), "chat_composer_codemirror");
  const mobile = useIsMobileViewport();
  const load = useCodeMirrorComposerLoad();
  const wanted = flagOn && !mobile;
  useEffect(() => {
    if (wanted) loadCodeMirrorComposer();
  }, [wanted]);
  useEffect(() => {
    if (!wanted || load.status !== "failed") return;
    let lastRetry = Date.now();
    const retry = () => {
      if (Date.now() - lastRetry < COMPOSER_LOAD_RETRY_INTERVAL_MS) return;
      lastRetry = Date.now();
      loadCodeMirrorComposer();
    };
    window.addEventListener("focus", retry);
    return () => window.removeEventListener("focus", retry);
  }, [wanted, load.status]);
  return effectiveComposerEngine(flagOn, mobile, load.status);
}
