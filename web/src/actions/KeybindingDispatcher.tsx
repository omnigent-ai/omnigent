import { useEffect, useRef } from "react";
import { DEFAULT_KEYBINDINGS } from "./defaultKeybindings";
import { matchingKeybindingRules, type KeybindingEnvironment } from "./keybindingResolver";
import { useInternalActionRuntime } from "./ActionProvider";
import type { ActionId, ActionInvocation, KeybindingRule } from "./types";
import { HANDLED } from "./types";

const CHORD_TIMEOUT_MS = 1_500;

interface PendingChord {
  rules: readonly KeybindingRule[];
  timer: number;
}

function isAltGraph(event: KeyboardEvent): boolean {
  return typeof event.getModifierState === "function" && event.getModifierState("AltGraph");
}

function isCompositionEvent(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229;
}

function consume(event: KeyboardEvent, rule: KeybindingRule): void {
  if (rule.preventDefault !== false) event.preventDefault();
  if (rule.stopPropagation) event.stopPropagation();
}

function invocationFor<A extends ActionId>(
  rule: KeybindingRule<A>,
  event: KeyboardEvent,
): ActionInvocation<A> {
  return {
    action: rule.action,
    args: rule.args,
    source: "keyboard",
    event,
  } as ActionInvocation<A>;
}

/** Installs the only application-owned global keyboard dispatch listeners. */
export function KeybindingDispatcher({
  rules = DEFAULT_KEYBINDINGS,
}: {
  rules?: readonly KeybindingRule[];
}) {
  const actions = useInternalActionRuntime();
  const pendingCapture = useRef<PendingChord | null>(null);
  const pendingBubble = useRef<PendingChord | null>(null);

  useEffect(() => {
    const handledInCapture = new WeakSet<KeyboardEvent>();

    const clearPending = (pending: { current: PendingChord | null }): void => {
      if (!pending.current) return;
      window.clearTimeout(pending.current.timer);
      pending.current = null;
    };

    const dispatch = (event: KeyboardEvent, phase: "capture" | "bubble"): void => {
      const pending = phase === "capture" ? pendingCapture : pendingBubble;
      if (event.key === "Escape") clearPending(pending);
      if (phase === "bubble" && handledInCapture.has(event)) {
        clearPending(pending);
        return;
      }
      if (isCompositionEvent(event) || isAltGraph(event)) {
        clearPending(pending);
        return;
      }

      const resolution = actions.getResolution(event);
      const environment: KeybindingEnvironment = {
        context: actions.registry.contextForResolution(resolution),
        focusedModes: actions.registry.getFocusedModes(resolution.focusedScopeIds),
        activeModes: actions.registry.getActiveModes(),
        focusedModeRanks: actions.registry.getFocusedModeRanks(resolution.focusedScopeIds),
        contextsForRule: (rule) => actions.registry.contextsForRule(rule, resolution),
      };

      if (pending.current) {
        const secondStroke = matchingKeybindingRules(
          pending.current.rules,
          event,
          1,
          phase,
          environment,
        );
        clearPending(pending);
        for (const rule of secondStroke) {
          if (!actions.registry.canHandle(rule.action, resolution, { keyboardOnly: true }))
            continue;
          const result = actions.registry.execute(invocationFor(rule, event), resolution);
          if (result !== HANDLED) continue;
          consume(event, rule);
          if (phase === "capture") handledInCapture.add(event);
          return;
        }
      }

      const candidates = matchingKeybindingRules(rules, event, 0, phase, environment).filter(
        (rule) => actions.registry.canHandle(rule.action, resolution, { keyboardOnly: true }),
      );

      // A chord prefix intentionally wins over a single-stroke binding with
      // the same first key, matching VS Code's wait-for-the-second-key model.
      const chords = candidates.filter((rule) => rule.sequence.length > 1);
      if (chords.length > 0) {
        const first = chords[0]!;
        pending.current = {
          rules: chords,
          timer: window.setTimeout(() => {
            pending.current = null;
          }, CHORD_TIMEOUT_MS),
        };
        consume(event, first);
        if (phase === "capture") handledInCapture.add(event);
        return;
      }

      for (const rule of candidates) {
        const result = actions.registry.execute(invocationFor(rule, event), resolution);
        if (result !== HANDLED) continue;
        consume(event, rule);
        if (phase === "capture") handledInCapture.add(event);
        return;
      }
    };

    const onCapture = (event: KeyboardEvent) => dispatch(event, "capture");
    const onBubble = (event: KeyboardEvent) => dispatch(event, "bubble");
    const clearChords = () => {
      clearPending(pendingCapture);
      clearPending(pendingBubble);
    };

    window.addEventListener("keydown", onCapture, true);
    window.addEventListener("keydown", onBubble);
    window.addEventListener("blur", clearChords);
    window.addEventListener("focusin", clearChords);
    document.addEventListener("visibilitychange", clearChords);
    return () => {
      window.removeEventListener("keydown", onCapture, true);
      window.removeEventListener("keydown", onBubble);
      window.removeEventListener("blur", clearChords);
      window.removeEventListener("focusin", clearChords);
      document.removeEventListener("visibilitychange", clearChords);
      clearChords();
    };
  }, [actions, rules]);

  return null;
}
