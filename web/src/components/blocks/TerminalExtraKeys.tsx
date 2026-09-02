// Termux-style extra-keys row rendered under the terminal on touch devices:
// a fixed 2×7 grid of Esc / Tab / arrows plus sticky Ctrl / Alt / Shift.
//
// Hot-path rules (the row must cost nothing while the user just types):
// modifier state lives in a ref the input transform reads; React state only
// changes on a modifier tap / long-press / consume, never per keystroke; the
// transform is installed on the session only while a modifier is active.

import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
  type PointerEvent,
} from "react";
import { cn } from "@/lib/utils";
import {
  EXTRA_KEY_ROWS,
  LONG_PRESS_MS,
  MODIFIERS_OFF,
  activeModifiers,
  encodeExtraKey,
  encodeModifiedInput,
  hasActiveModifier,
  reduceModifiers,
  type ExtraKeyDef,
  type ModifierStates,
} from "./terminalExtraKeysModel";

/** Rewrite applied to each typed ``onData`` chunk while a modifier is active. */
export type ExtraKeysInputTransform = (data: string) => string;

/**
 * The slice of the live terminal bridge the row drives. Read at event time
 * (never captured), so a re-dialed session needs no re-render of the row.
 */
export interface ExtraKeysTarget {
  /** Send raw bytes as one frame, bypassing the input transform. */
  send: (data: string) => void;
  /** Install or clear (``null``) the typed-input rewrite on the live session. */
  setTransform: (fn: ExtraKeysInputTransform | null) => void;
  /** Focus xterm's textarea so the next soft-keyboard character combines. */
  focus: () => void;
  /** Whether the program enabled application cursor keys (DECCKM). */
  applicationCursor: () => boolean;
}

interface TerminalExtraKeysProps {
  target: ExtraKeysTarget;
  className?: string;
}

interface PressState {
  pointerId: number;
  key: ExtraKeyDef;
  timer: number | null;
  longPressFired: boolean;
}

export const TerminalExtraKeys = memo(function TerminalExtraKeys({
  target,
  className,
}: TerminalExtraKeysProps) {
  const [mods, setMods] = useState<ModifierStates>(MODIFIERS_OFF);
  const modsRef = useRef<ModifierStates>(MODIFIERS_OFF);
  const pressRef = useRef<PressState | null>(null);
  const targetRef = useRef(target);
  targetRef.current = target;
  const transformRef = useRef<ExtraKeysInputTransform | null>(null);

  // Single write point for modifier state: the ref feeds the transform, the
  // React state repaints the highlighted keys, and the session only carries
  // a transform while something is armed or locked.
  const commit = useCallback((next: ModifierStates) => {
    if (next === modsRef.current) return;
    modsRef.current = next;
    setMods(next);
    targetRef.current.setTransform(hasActiveModifier(next) ? transformRef.current : null);
  }, []);

  // Reads the ref, not React state, so a keystroke never waits on a render.
  const transform = useCallback<ExtraKeysInputTransform>(
    (data) => {
      const state = modsRef.current;
      const out = encodeModifiedInput(data, activeModifiers(state));
      commit(reduceModifiers(state, { type: "consume" }));
      return out;
    },
    [commit],
  );
  transformRef.current = transform;

  const clearPress = useCallback(() => {
    const press = pressRef.current;
    if (press?.timer != null) window.clearTimeout(press.timer);
    pressRef.current = null;
  }, []);

  // Clear the rewrite and any pending long-press when the row unmounts (the
  // visibility preference flips, the pointer turns fine, the view closes).
  useEffect(() => {
    return () => {
      clearPress();
      targetRef.current.setTransform(null);
    };
  }, [clearPress]);

  const activate = useCallback(
    (key: ExtraKeyDef) => {
      if (key.kind === "modifier") {
        commit(reduceModifiers(modsRef.current, { type: "tap", mod: key.id }));
        return;
      }
      const state = modsRef.current;
      const seq = encodeExtraKey(key.id, activeModifiers(state), {
        applicationCursor: targetRef.current.applicationCursor(),
      });
      targetRef.current.send(seq);
      commit(reduceModifiers(state, { type: "consume" }));
    },
    [commit],
  );

  const onPointerDown = useCallback(
    (e: PointerEvent<HTMLButtonElement>, key: ExtraKeyDef) => {
      if (e.button !== 0 || !e.isPrimary) return;
      // Keep focus where it is (xterm's textarea, so the soft keyboard stays
      // open) and suppress the compat mousedown/click pair.
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      clearPress();
      const press: PressState = { pointerId: e.pointerId, key, timer: null, longPressFired: false };
      if (key.kind === "modifier") {
        // WKWebView honors a programmatic focus only inside the trusted
        // gesture; plain keys never focus (Esc must not summon the keyboard).
        targetRef.current.focus();
        press.timer = window.setTimeout(() => {
          press.timer = null;
          press.longPressFired = true;
          commit(reduceModifiers(modsRef.current, { type: "longPress", mod: key.id }));
        }, LONG_PRESS_MS);
      }
      pressRef.current = press;
    },
    [clearPress, commit],
  );

  const onPointerUp = useCallback(
    (e: PointerEvent<HTMLButtonElement>) => {
      const press = pressRef.current;
      if (!press || press.pointerId !== e.pointerId) return;
      clearPress();
      if (!press.longPressFired) activate(press.key);
    },
    [activate, clearPress],
  );

  const onPointerCancel = useCallback(
    (e: PointerEvent<HTMLButtonElement>) => {
      if (pressRef.current?.pointerId === e.pointerId) clearPress();
    },
    [clearPress],
  );

  const onClick = useCallback(
    (e: MouseEvent<HTMLButtonElement>, key: ExtraKeyDef) => {
      // Pointer taps are handled on pointerup; only keyboard / assistive-tech
      // activation (a synthesized click, detail 0) reaches here.
      if (e.detail !== 0) return;
      activate(key);
    },
    [activate],
  );

  return (
    <div
      role="toolbar"
      aria-label="Terminal extra keys"
      data-testid="terminal-extra-keys"
      className={cn("terminal-extra-keys", className)}
      onContextMenu={(e) => e.preventDefault()}
    >
      {EXTRA_KEY_ROWS.map((row, rowIndex) =>
        row.map((key) => {
          const modState = key.kind === "modifier" ? mods[key.id] : undefined;
          return (
            <button
              key={key.id}
              type="button"
              tabIndex={-1}
              aria-label={key.name}
              aria-pressed={modState === undefined ? undefined : modState !== "off"}
              data-key={key.id}
              data-row={rowIndex + 1}
              data-modifier-state={modState}
              onPointerDown={(e) => onPointerDown(e, key)}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerCancel}
              onClick={(e) => onClick(e, key)}
            >
              {key.label}
            </button>
          );
        }),
      )}
    </div>
  );
});
