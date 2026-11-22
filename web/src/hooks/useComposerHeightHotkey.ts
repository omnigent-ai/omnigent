// ⌘K's "Expand message box" / "Collapse message box": it acts on the message box focus entered
// last, else the newest. Every composer that can expand mounts this; only the one the command
// acts on offers the row, so ⌘K lists it once however many are up.

import { Maximize2Icon, Minimize2Icon } from "lucide-react";
import { type RefObject, useEffect, useId, useMemo, useRef, useSyncExternalStore } from "react";

import {
  composerHeightOf,
  DEFAULT_COMPOSER_HEIGHT,
  subscribeComposerHeights,
  toggleComposerHeight,
  type ComposerHeight,
} from "@/lib/composerHeightState";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

/** The palette sources of the message box's height row start with this, one per composer. */
export const COMPOSER_HEIGHT_COMMAND_SOURCE = "composer-height";

/** What the toggle does next: its button label and ⌘K row. */
export function composerHeightActionLabel(height: ComposerHeight): string {
  return height === "expanded" ? "Collapse message box" : "Expand message box";
}

/** A mounted composer that can expand: its height scope, and when focus last entered it. */
interface MountedComposer {
  scope: string;
  /** 0 until focus first enters it; mounting alone never counts as focus. */
  focusedAt: number;
}

const mountedComposers: MountedComposer[] = [];
let focusCount = 0;
const targetListeners = new Set<() => void>();

function targetChanged(): void {
  for (const listener of targetListeners) listener();
}

function subscribeTarget(listener: () => void): () => void {
  targetListeners.add(listener);
  return () => {
    targetListeners.delete(listener);
  };
}

/**
 * The composer ⌘K acts on: the one focus entered last, which is the one holding focus while
 * any does; else the newest; null with none mounted. It changes only as focus enters a
 * composer or one mounts or unmounts, so the row and its action always agree.
 */
function targetComposer(): MountedComposer | null {
  let target: MountedComposer | null = null;
  for (const composer of mountedComposers) {
    if (!target || composer.focusedAt > target.focusedAt) target = composer;
  }
  // Nothing focused yet: the newest composer.
  if (target && target.focusedAt === 0) return mountedComposers.at(-1) ?? null;
  return target;
}

/** The height scope ⌘K acts on now, or null with no composer that can expand. */
export function targetComposerScope(): string | null {
  return targetComposer()?.scope ?? null;
}

/**
 * Offer ⌘K's height row while this composer, showing `scope`, is mounted and can expand. The
 * row names what it does to the composer it acts on, which may be another one.
 */
export function useComposerHeightHotkey({
  scope,
  enabled,
  root,
}: {
  scope: string;
  enabled: boolean;
  root: RefObject<HTMLElement | null>;
}): void {
  // One entry for the composer's life: a session switch changes its scope, not when focus last
  // entered it.
  const composer = useRef<MountedComposer>({ scope, focusedAt: 0 }).current;
  // Each composer offers under its own source, so one withdrawing never takes another's row.
  const source = `${COMPOSER_HEIGHT_COMMAND_SOURCE}:${useId()}`;
  const isTarget = useSyncExternalStore(
    subscribeTarget,
    () => targetComposer() === composer,
    () => false,
  );
  const height = useSyncExternalStore(
    subscribeComposerHeights,
    () => composerHeightOf(scope),
    () => DEFAULT_COMPOSER_HEIGHT,
  );

  useEffect(() => {
    if (composer.scope === scope) return;
    composer.scope = scope;
    targetChanged();
  }, [composer, scope]);

  useEffect(() => {
    if (!enabled) return;
    const element = root.current;
    // A composer mounting with focus already in it, as on autofocus, is the one focused last.
    const focused = document.activeElement;
    if (focused instanceof Node && element?.contains(focused)) composer.focusedAt = ++focusCount;
    const onFocusIn = () => {
      composer.focusedAt = ++focusCount;
      targetChanged();
    };
    element?.addEventListener("focusin", onFocusIn);
    mountedComposers.push(composer);
    targetChanged();
    return () => {
      element?.removeEventListener("focusin", onFocusIn);
      mountedComposers.splice(mountedComposers.indexOf(composer), 1);
      targetChanged();
    };
  }, [composer, enabled, root]);

  const commands = useMemo<PaletteCommand[]>(
    () =>
      isTarget
        ? [
            {
              id: `composer-toggle-height:${scope}`,
              label: composerHeightActionLabel(height),
              icon: height === "expanded" ? Minimize2Icon : Maximize2Icon,
              keywords: ["message box", "composer", "prompt", "taller", "maximize", "minimize"],
              run: () => toggleComposerHeight(scope),
            },
          ]
        : [],
    [isTarget, scope, height],
  );
  usePaletteCommands(source, "Message", commands);
}
