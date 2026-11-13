// Tracks whether the command modifier (⌘ on macOS, Ctrl elsewhere) is held, so shortcut hints
// can show. Shift, Control beside ⌘ on macOS, and Alt in a browser tab (whose session chord is
// ⌘⌥digit) may join the hold; any other key, including AltGraph, ends it.

import { useEffect } from "react";
import { create } from "zustand";
import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";

/** Hold time before hints show, so a quick chord such as ⌘C never flashes them. */
export const COMMAND_HOLD_REVEAL_DELAY_MS = 400;

interface CommandHoldState {
  /** True once the command modifier has been held for the reveal delay. */
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
    // Modifier keys that may be down during a hold. Off macOS, Meta opens OS menus.
    const joiningKeys = new Set([commandKey, "Shift"]);
    if (isMac) joiningKeys.add("Control");
    if (altJoinsHold) joiningKeys.add("Alt");
    let revealTimer: ReturnType<typeof setTimeout> | undefined;
    // Other keys still down, which block the hints: those pressed without the
    // modifier, and those pressed during a chord (macOS may drop their keyup).
    const heldKeys = new Set<string>();
    const chordKeys = new Set<string>();
    const keyId = (e: globalThis.KeyboardEvent): string => e.code || e.key;
    // A hold canceled by another key stays canceled until the command key is released or
    // pressed afresh, so a joining modifier pressed afterwards cannot revive it.
    let holdCanceled = false;

    const clearHold = (): void => {
      clearTimeout(revealTimer);
      revealTimer = undefined;
      setHeld(false);
    };
    const reset = (): void => {
      heldKeys.clear();
      chordKeys.clear();
      holdCanceled = false;
      clearHold();
    };
    // The command modifier down, with only joining modifiers beside it.
    const isCommandHold = (e: globalThis.KeyboardEvent): boolean => {
      const commandDown = isMac ? e.metaKey : e.ctrlKey && !e.metaKey;
      if (!commandDown) return false;
      if (e.altKey && !altJoinsHold) return false;
      // AltGr reports as Ctrl+Alt on Windows/Linux intl layouts. Feature-detect
      // like useSessionNumberHotkeys so synthetic events without the method work.
      const altGraph = typeof e.getModifierState === "function" && e.getModifierState("AltGraph");
      return !altGraph;
    };
    const onKeyDown = (e: globalThis.KeyboardEvent): void => {
      // A non-repeat command keydown starts a new hold, recovering from a missed keyup.
      if (e.key === commandKey && !e.repeat) holdCanceled = false;
      const joinsHold = joiningKeys.has(e.key);
      // The physical modifier, not shortcut eligibility: ⌃⌘K still drops K's keyup.
      const commandDown = isMac ? e.metaKey : e.ctrlKey;
      if (!joinsHold && !heldKeys.has(keyId(e))) {
        (commandDown ? chordKeys : heldKeys).add(keyId(e));
      }
      if (!joinsHold || !isCommandHold(e)) {
        if (commandDown) holdCanceled = true;
        clearHold();
        return;
      }
      const keysDown = heldKeys.size + chordKeys.size;
      const idle = revealTimer === undefined && !useCommandHoldStore.getState().held;
      if (keysDown === 0 && idle && !holdCanceled) {
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
        holdCanceled = false;
        clearHold();
      }
      // Releasing a joining modifier keeps the hold. A keyup never starts one.
      else if (!isCommandHold(e)) clearHold();
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
