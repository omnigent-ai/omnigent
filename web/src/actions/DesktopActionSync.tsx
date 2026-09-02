import { useEffect, useMemo, useRef } from "react";
import {
  clearDesktopActionBindings,
  onDesktopActionInvoked,
  reportDesktopActionResult,
  setDesktopActionBindings,
} from "@/lib/nativeBridge";
import { useInternalActionRuntime } from "./ActionProvider";
import { desktopActionBindingSnapshot, isDesktopMenuAction } from "./desktopActionBridge";
import { isMacKeyboardPlatform } from "./keybindingEnvironment";
import { useKeybindingSnapshot } from "./KeybindingStore";
import { HANDLED } from "./types";

/** Keep native menu accelerators and invocations synchronized with one window. */
export function DesktopActionSync() {
  const runtime = useInternalActionRuntime();
  const keymap = useKeybindingSnapshot();
  const snapshot = useMemo(
    () => desktopActionBindingSnapshot(keymap.effectiveRules, isMacKeyboardPlatform()),
    [keymap.effectiveRules],
  );
  const serialized = useMemo(() => JSON.stringify(snapshot), [snapshot]);
  const lastPublished = useRef<string | null>(null);

  useEffect(() => {
    if (lastPublished.current === serialized) return;
    if (setDesktopActionBindings(snapshot)) lastPublished.current = serialized;
  }, [serialized, snapshot]);

  useEffect(
    () => () => {
      lastPublished.current = null;
      clearDesktopActionBindings();
    },
    [],
  );

  useEffect(
    () =>
      onDesktopActionInvoked(({ action, requestId }) => {
        if (!isDesktopMenuAction(action)) {
          reportDesktopActionResult(requestId, false);
          return;
        }
        let handled = false;
        try {
          const resolution = runtime.getResolution();
          handled =
            runtime.registry.canHandle(action, resolution) &&
            runtime.registry.execute({ action, source: "menu" }, resolution) === HANDLED;
        } catch (error) {
          console.error(`Native action ${action} failed`, error);
        }
        reportDesktopActionResult(requestId, handled);
      }),
    [runtime],
  );

  return null;
}
