// ⌘K's "Expand message box" / "Collapse message box", and ⇧⌘M in the macOS desktop app: they
// act on the message box focus entered last, else the newest. Every composer that can expand
// mounts this; only the one the command acts on offers the row, so ⌘K lists it once however
// many are up, and the chord is heard once per window.

import { Maximize2Icon, Minimize2Icon } from "lucide-react";
import { type RefObject, useEffect, useId, useMemo, useRef, useSyncExternalStore } from "react";

import {
  toggleComposerHeight,
  useComposerHeight,
  type ComposerHeight,
} from "@/lib/composerHeightState";
import { matchesShortcut, shortcutKeys } from "@/lib/shortcuts/registry";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

/** The palette sources of the message box's height row start with this, one per composer. */
export const COMPOSER_HEIGHT_COMMAND_SOURCE = "composer-height";

/** What the toggle does next: its button label and ⌘K row. */
export function composerHeightActionLabel(height: ComposerHeight): string {
  return height === "expanded" ? "Collapse message box" : "Expand message box";
}

/** The chord's keys, for the toggle's tooltip; none where it isn't bound. */
export function composerHeightKeys(): string[] {
  return shortcutKeys("composer.toggleHeight");
}

/** Surfaces that keep their own keys, such as a terminal or the file editor. */
const KEY_OWNING_SURFACES = ".xterm, .monaco-editor";

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

function onHeightChord(event: KeyboardEvent): void {
  if (event.repeat || !matchesShortcut(event, "composer.toggleHeight")) return;
  const focused = document.activeElement;
  if (focused instanceof Element && focused.closest(KEY_OWNING_SURFACES)) return;
  const scope = targetComposerScope();
  if (scope === null) return;
  event.preventDefault();
  event.stopPropagation();
  toggleComposerHeight(scope);
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
  const height = useComposerHeight(scope);

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
    // The first composer up listens for the chord, and the last one down stops.
    if (mountedComposers.length === 0) window.addEventListener("keydown", onHeightChord);
    mountedComposers.push(composer);
    targetChanged();
    return () => {
      element?.removeEventListener("focusin", onFocusIn);
      mountedComposers.splice(mountedComposers.indexOf(composer), 1);
      if (mountedComposers.length === 0) window.removeEventListener("keydown", onHeightChord);
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
              shortcut: "composer.toggleHeight",
              run: () => toggleComposerHeight(scope),
            },
          ]
        : [],
    [isTarget, scope, height],
  );
  usePaletteCommands(source, "Message", commands);
}
