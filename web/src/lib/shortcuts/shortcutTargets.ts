// Which target a chord acts on right now, for chords that act on one thing, such as
// ⌘⌥R renaming the session in the header. The code that binds the chord reports its
// target, so menus and ⌘K show the chord only on rows it would act on.

import { useEffect } from "react";
import { create } from "zustand";

import type { ShortcutId } from "./registry";

interface ShortcutTargetsState {
  /** Binding → the target its chord acts on now; absent while nothing binds it. */
  targets: Partial<Record<ShortcutId, string>>;
}

export const shortcutTargetsStore = create<ShortcutTargetsState>(() => ({ targets: {} }));

/** Report that `id`'s chord acts on `target` while this is mounted and `enabled`. */
export function useBindShortcutTarget(
  id: ShortcutId,
  target: string | null | undefined,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled || !target) return;
    shortcutTargetsStore.setState((state) => ({ targets: { ...state.targets, [id]: target } }));
    return () => {
      shortcutTargetsStore.setState((state) => {
        // A newer binder may already have taken the chord over.
        if (state.targets[id] !== target) return state;
        const { [id]: _released, ...rest } = state.targets;
        return { targets: rest };
      });
    };
  }, [id, target, enabled]);
}

/** `id` while its chord acts on `target`, for a menu row about `target`; otherwise undefined. */
export function useTargetedShortcut(
  id: ShortcutId,
  target: string | null | undefined,
): ShortcutId | undefined {
  const bound = shortcutTargetsStore((state) => state.targets[id]);
  return target && bound === target ? id : undefined;
}

/** Every chord's current target, for a surface that checks several bindings at once. */
export function useShortcutTargets(): Partial<Record<ShortcutId, string>> {
  return shortcutTargetsStore((state) => state.targets);
}
