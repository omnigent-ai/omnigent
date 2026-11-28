// ⇧J / ⇧K on the PR page: down and up its stack, as on github.com. Typing keeps its letters,
// an open dialog, menu or overlay keeps its keys, and a step that can't run leaves the key alone.

import { useEffect, useRef } from "react";

import { isFocusInDialogOrMenu, isKeyOwningOverlayOpen, isMacPlatform } from "@/lib/hotkeys";
import { focusedKeyOwner } from "@/lib/shortcuts/editorOwnedChords";
import { matchesShortcut, type ShortcutId } from "@/lib/shortcuts/registry";
import { useOwnsPaneCommands } from "@/store/PaletteCommandScope";

export type StackStepDirection = "previous" | "next";

/** Each direction's binding, shared with its ⌘K row. */
export const STACK_SHORTCUTS = {
  previous: "pullRequestStack.previous",
  next: "pullRequestStack.next",
} as const satisfies Record<StackStepDirection, ShortcutId>;

const DIRECTIONS: readonly StackStepDirection[] = ["previous", "next"];

/**
 * Bind the stack keys while `active` and this pane owns commands, as its ⌘K rows do: of two PR
 * pages on screen, only the focused one's stack moves. Each key runs `step` when `canStep` lets it.
 */
export function usePullRequestStackKeys(
  {
    active,
    canStep,
    step,
  }: {
    active: boolean;
    canStep: (direction: StackStepDirection) => boolean;
    step: (direction: StackStepDirection) => void;
  },
  mac: boolean = isMacPlatform(),
): void {
  const ownsKeys = useOwnsPaneCommands();
  const latest = useRef({ canStep, step });
  latest.current = { canStep, step };
  useEffect(() => {
    if (!active || !ownsKeys) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) return;
      if (focusedKeyOwner() !== null || isFocusInDialogOrMenu() || isKeyOwningOverlayOpen()) {
        return;
      }
      const direction = DIRECTIONS.find((candidate) =>
        matchesShortcut(event, STACK_SHORTCUTS[candidate], { mac }),
      );
      if (!direction || !latest.current.canStep(direction)) return;
      event.preventDefault();
      latest.current.step(direction);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, ownsKeys, mac]);
}
