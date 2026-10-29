// Tracks whether the platform command modifier (⌘ on macOS, Ctrl elsewhere) is
// held on its own, so shortcut hints can show while it is down. Alt may join the
// hold; any other key, Shift, or AltGraph ends it.

import { useEffect } from "react";
import { create } from "zustand";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

/** Hold time before hints show, so a quick chord such as ⌘C never flashes them. */
export const COMMAND_HOLD_REVEAL_DELAY_MS = 400;

interface CommandHoldState {
  /** True once the command modifier has been held alone for the reveal delay. */
  held: boolean;
}

/** Subscribers select `held` so only they re-render when a hold starts or ends. */
export const useCommandHoldStore = create<CommandHoldState>(() => ({ held: false }));

function setHeld(held: boolean): void {
  if (useCommandHoldStore.getState().held !== held) useCommandHoldStore.setState({ held });
}

/**
 * Installs the window listeners that drive {@link useCommandHoldStore}. Mount once.
 * @param isMac Injectable so tests can pin the platform.
 */
export function useCommandHoldTracking(isMac = isMacPlatform()): void {
  useEffect(() => {
    const commandKey = isMac ? "Meta" : "Control";
    let revealTimer: ReturnType<typeof setTimeout> | undefined;

    const clearHold = (): void => {
      clearTimeout(revealTimer);
      revealTimer = undefined;
      setHeld(false);
    };
    // The command modifier with at most Alt: no Shift, no AltGraph.
    const isCommandOnlyHold = (e: globalThis.KeyboardEvent): boolean => {
      if (e.shiftKey || !hasCommandModifier(e, isMac)) return false;
      // AltGr reports as Ctrl+Alt on Windows/Linux intl layouts. Feature-detect
      // like usePinnedSessionHotkeys so synthetic events without the method work.
      const altGraph = typeof e.getModifierState === "function" && e.getModifierState("AltGraph");
      return !altGraph;
    };
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      // Only the command modifier and Alt may join a hold; any other key ends it.
      if ((e.key !== commandKey && e.key !== "Alt") || !isCommandOnlyHold(e)) {
        clearHold();
        return;
      }
      if (revealTimer === undefined && !useCommandHoldStore.getState().held) {
        revealTimer = setTimeout(() => {
          revealTimer = undefined;
          setHeld(true);
        }, COMMAND_HOLD_REVEAL_DELAY_MS);
      }
    };
    const onKeyUp = (e: globalThis.KeyboardEvent): void => {
      // Releasing Alt keeps the hold. A keyup never starts one.
      if (e.key === commandKey || !isCommandOnlyHold(e)) clearHold();
    };
    const onVisibilityChange = (): void => {
      if (document.visibilityState === "hidden") clearHold();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", clearHold);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", clearHold);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      clearHold();
    };
  }, [isMac]);
}
