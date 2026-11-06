// Tracks whether the platform command modifier (⌘ on macOS, Ctrl elsewhere) is
// held on its own, so shortcut hints can show while it is down. In a browser tab,
// where the session chord is ⌘⌥digit, Alt may join the hold; in the desktop app
// Alt ends it like any other key. Shift and AltGraph always end it.

import { useEffect } from "react";
import { create } from "zustand";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";

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
 * @param altJoinsHold Whether Alt may join the hold: true in a browser tab, whose
 *   session chord adds Alt, false in the desktop app.
 */
export function useCommandHoldTracking(
  isMac = isMacPlatform(),
  altJoinsHold = !isNativeShell(),
): void {
  useEffect(() => {
    const commandKey = isMac ? "Meta" : "Control";
    let revealTimer: ReturnType<typeof setTimeout> | undefined;
    // Other keys still down, which block the hints: those pressed without the
    // modifier, and those pressed during a chord (macOS may drop their keyup).
    const heldKeys = new Set<string>();
    const chordKeys = new Set<string>();
    const keyId = (e: globalThis.KeyboardEvent): string => e.code || e.key;

    const clearHold = (): void => {
      clearTimeout(revealTimer);
      revealTimer = undefined;
      setHeld(false);
    };
    const reset = (): void => {
      heldKeys.clear();
      chordKeys.clear();
      clearHold();
    };
    // The command modifier, with Alt only where it joins: no Shift, no AltGraph.
    const isCommandOnlyHold = (e: globalThis.KeyboardEvent): boolean => {
      if (e.shiftKey || !hasCommandModifier(e, isMac)) return false;
      if (e.altKey && !altJoinsHold) return false;
      // AltGr reports as Ctrl+Alt on Windows/Linux intl layouts. Feature-detect
      // like useSessionNumberHotkeys so synthetic events without the method work.
      const altGraph = typeof e.getModifierState === "function" && e.getModifierState("AltGraph");
      return !altGraph;
    };
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      // Only the command modifier (and Alt, where it joins) may join a hold.
      const joinsHold = e.key === commandKey || (altJoinsHold && e.key === "Alt");
      if (!joinsHold && !heldKeys.has(keyId(e))) {
        // The physical modifier, not shortcut eligibility: ⌃⌘K still drops K's keyup.
        const commandDown = isMac ? e.metaKey : e.ctrlKey;
        (commandDown ? chordKeys : heldKeys).add(keyId(e));
      }
      if (!joinsHold || !isCommandOnlyHold(e)) {
        clearHold();
        return;
      }
      const keysDown = heldKeys.size + chordKeys.size;
      if (keysDown === 0 && revealTimer === undefined && !useCommandHoldStore.getState().held) {
        revealTimer = setTimeout(() => {
          revealTimer = undefined;
          setHeld(true);
        }, COMMAND_HOLD_REVEAL_DELAY_MS);
      }
    };
    const onKeyUp = (e: globalThis.KeyboardEvent): void => {
      heldKeys.delete(keyId(e));
      chordKeys.delete(keyId(e));
      if (e.key === commandKey) {
        // macOS sends no keyup for keys released while ⌘ is down, so its release
        // forgets the chord's keys. Keys held from before ⌘ still block.
        if (isMac) chordKeys.clear();
        clearHold();
      }
      // Releasing a joining Alt keeps the hold. A keyup never starts one.
      else if (!isCommandOnlyHold(e)) clearHold();
    };
    const onVisibilityChange = (): void => {
      if (document.visibilityState === "hidden") reset();
    };

    // Capture phase, so shortcuts that other handlers stop still cancel the hints.
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", reset);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", reset);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      reset();
    };
  }, [isMac, altJoinsHold]);
}
