import { useEffect } from "react";
import { useKeybindingSnapshot } from "./KeybindingStore";
import { matchingKeybindingRules, type KeybindingEnvironment } from "./keybindingResolver";
import { useInternalActionRuntime, useKeybindingDispatchSuspended } from "./ActionProvider";
import type { ActionId, ActionInvocation, KeybindingRule } from "./types";
import { HANDLED } from "./types";

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
export function KeybindingDispatcher({ rules }: { rules?: readonly KeybindingRule[] }) {
  const actions = useInternalActionRuntime();
  const suspended = useKeybindingDispatchSuspended();
  const storedKeymap = useKeybindingSnapshot();
  const effectiveRules = rules ?? storedKeymap.effectiveRules;

  useEffect(() => {
    if (suspended) return;
    const handledInCapture = new WeakSet<KeyboardEvent>();

    const dispatch = (event: KeyboardEvent, phase: "capture" | "bubble"): void => {
      if (actions.dispatchSuspension.getSnapshot()) return;
      if (phase === "bubble" && handledInCapture.has(event)) return;
      if (isCompositionEvent(event) || isAltGraph(event)) return;

      const resolution = actions.getResolution(event);
      const environment: KeybindingEnvironment = {
        context: actions.registry.contextForResolution(resolution),
        focusedModes: actions.registry.getFocusedModes(resolution.focusedScopeIds),
        activeModes: actions.registry.getActiveModes(),
        focusedModeRanks: actions.registry.getFocusedModeRanks(resolution.focusedScopeIds),
        contextsForRule: (rule) => actions.registry.contextsForRule(rule, resolution),
      };

      const candidates = matchingKeybindingRules(
        effectiveRules,
        event,
        0,
        phase,
        environment,
      ).filter((rule) =>
        actions.registry.canHandle(rule.action, resolution, { keyboardOnly: true }),
      );

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

    window.addEventListener("keydown", onCapture, true);
    window.addEventListener("keydown", onBubble);
    return () => {
      window.removeEventListener("keydown", onCapture, true);
      window.removeEventListener("keydown", onBubble);
    };
  }, [actions, effectiveRules, suspended]);

  return null;
}
