import { contextSpecificity, evaluateContext } from "./context";
import type { ContextSnapshot, KeybindingMode, KeybindingRule, KeyStroke } from "./types";

export interface KeybindingEnvironment {
  context: ContextSnapshot;
  focusedModes: ReadonlySet<KeybindingMode>;
  activeModes: ReadonlySet<KeybindingMode>;
  contextsForRule?: (rule: KeybindingRule) => readonly ContextSnapshot[];
}

function logicalEventKey(key: string): string {
  return [...key].length === 1 ? key.toLocaleLowerCase() : key;
}

export function keyStrokeMatchesEvent(
  stroke: KeyStroke,
  event: KeyboardEvent,
  isMac: boolean,
): boolean {
  if (stroke.key.kind === "code") {
    if (event.code !== stroke.key.value) return false;
  } else if (logicalEventKey(event.key) !== stroke.key.value) {
    return false;
  }

  const modifiers = new Set(stroke.modifiers);
  if (event.altKey !== modifiers.has("alt") || event.shiftKey !== modifiers.has("shift")) {
    return false;
  }

  if (modifiers.has("primary")) {
    if (!event.metaKey && !event.ctrlKey) return false;
  } else if (modifiers.has("mod")) {
    if (isMac) {
      if (!event.metaKey || event.ctrlKey) return false;
    } else if (!event.ctrlKey || event.metaKey) {
      return false;
    }
  } else {
    if (event.ctrlKey !== modifiers.has("ctrl")) return false;
    if (event.metaKey !== modifiers.has("meta")) return false;
  }

  return true;
}

export function ruleModeMatches(
  rule: KeybindingRule,
  focusedModes: ReadonlySet<KeybindingMode>,
  activeModes: ReadonlySet<KeybindingMode>,
): boolean {
  if (rule.mode === "global") return true;
  return (rule.activation ?? "focused") === "active"
    ? activeModes.has(rule.mode)
    : focusedModes.has(rule.mode);
}

export function matchingKeybindingRules(
  rules: readonly KeybindingRule[],
  event: KeyboardEvent,
  strokeIndex: number,
  phase: "capture" | "bubble",
  environment: KeybindingEnvironment,
): KeybindingRule[] {
  return rules
    .map((rule, index) => ({ rule, index }))
    .filter(({ rule }) => (rule.phase ?? "bubble") === phase)
    .filter(({ rule }) => rule.allowDefaultPrevented === true || !event.defaultPrevented)
    .filter(({ rule }) => rule.allowRepeat === true || !event.repeat)
    .filter(({ rule }) => rule.sequence[strokeIndex] !== undefined)
    .filter(({ rule }) =>
      keyStrokeMatchesEvent(rule.sequence[strokeIndex]!, event, environment.context.isMac),
    )
    .filter(({ rule }) => ruleModeMatches(rule, environment.focusedModes, environment.activeModes))
    .filter(({ rule }) =>
      (environment.contextsForRule?.(rule) ?? [environment.context]).some((context) =>
        evaluateContext(rule.when, context),
      ),
    )
    .sort(
      (left, right) =>
        (right.rule.priority ?? 0) - (left.rule.priority ?? 0) ||
        contextSpecificity(right.rule.when) - contextSpecificity(left.rule.when) ||
        right.index - left.index,
    )
    .map(({ rule }) => rule);
}
