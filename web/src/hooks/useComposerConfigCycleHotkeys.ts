import { useCallback, useEffect, useRef, useState } from "react";

import type { ComposerConfigChoice } from "@/components/composer/ComposerConfigSections";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isImeCompositionKeyEvent } from "@/lib/ime";

/** Ctrl+Alt+M/E cycles the same model/effort choices offered by the composer menu. */
export function useComposerConfigCycleHotkeys(
  models: readonly ComposerConfigChoice[] | undefined,
  efforts: readonly ComposerConfigChoice[] | undefined,
  enabled = true,
  pending = false,
  deferred?: { apply: () => void; cancel: () => void },
): { preview: "models" | "efforts" | null; dismissPreview: () => void } {
  const latest = useRef({ models, efforts });
  latest.current = { models, efforts };
  const deferredRef = useRef(deferred);
  deferredRef.current = deferred;
  const hasDeferred = deferred !== undefined;
  const isEmbedded = useIsEmbedded();
  const [previewRequest, setPreviewRequest] = useState<{ section: "models" | "efforts" } | null>(
    null,
  );
  const preview = previewRequest?.section ?? null;
  const checkedKey = (preview === "models" ? models : efforts)?.find(
    (choice) => choice.checked,
  )?.key;
  const dismissPreview = useCallback(() => {
    deferredRef.current?.cancel();
    setPreviewRequest(null);
  }, []);

  useEffect(() => {
    if (!previewRequest || pending) return;
    const timeout = window.setTimeout(() => {
      if (deferredRef.current) deferredRef.current.apply();
      else dismissPreview();
    }, 2_000);
    return () => window.clearTimeout(timeout);
  }, [previewRequest, checkedKey, dismissPreview, pending]);

  useEffect(() => {
    if (!previewRequest || !hasDeferred) return;
    const confirm = (event: globalThis.KeyboardEvent) => {
      if (
        event.key !== "Enter" ||
        event.defaultPrevented ||
        event.shiftKey ||
        event.altKey ||
        isImeCompositionKeyEvent({ nativeEvent: event }) ||
        !(event.target instanceof Element) ||
        !event.target.closest("[data-composer-card] textarea, [data-cycle-preview]")
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) deferredRef.current?.apply();
    };
    document.addEventListener("keydown", confirm, true);
    return () => document.removeEventListener("keydown", confirm, true);
  }, [previewRequest, hasDeferred]);

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (
        event.defaultPrevented ||
        event.repeat ||
        isImeCompositionKeyEvent({ nativeEvent: event }) ||
        !event.ctrlKey ||
        !event.altKey ||
        event.metaKey ||
        event.shiftKey ||
        event.getModifierState("AltGraph")
      )
        return;
      // Ctrl on macOS too: Cmd+Alt+M minimizes every window.
      const choices =
        event.code === "KeyM"
          ? latest.current.models
          : event.code === "KeyE"
            ? latest.current.efforts
            : undefined;
      if (!choices?.length) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      const focused = document.activeElement;
      if (
        focused instanceof Element &&
        focused.closest('.xterm, .monaco-editor, [role="dialog"], [role="alertdialog"]')
      )
        return;
      const current = choices.findIndex((choice) => choice.checked);
      for (let step = 1; step <= choices.length; step += 1) {
        const choice = choices[(current + step) % choices.length]!;
        if (choice.checked || choice.disabled || !choice.onSelect) continue;
        event.preventDefault();
        event.stopPropagation();
        choice.onSelect();
        setPreviewRequest({ section: event.code === "KeyM" ? "models" : "efforts" });
        return;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isEmbedded]);
  return { preview, dismissPreview };
}
