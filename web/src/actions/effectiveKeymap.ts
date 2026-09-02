import { ACTIONS_BY_ID } from "./catalog";
import { contextSpecificity, contextsMayOverlap } from "./context";
import type { UserKeybindingRule } from "./keybindingPreferences";
import { parseKeybinding, serializeKeybinding } from "./keybindingParser";
import type { ActionArgsById, ActionId, JsonValue, KeybindingMode, KeybindingRule } from "./types";

export interface KeybindingConflictRule {
  id: string;
  action: ActionId;
  mode: KeybindingMode;
  origin: "default" | "user";
}

type KeybindingConflictReason = "chordPrefix" | "priority" | "specificity" | "user" | "later";

interface KeybindingConflictBase {
  /** The exact sequence or shared chord prefix that collides. */
  sequence: string;
  kind: "exact" | "chordPrefix";
  first: KeybindingConflictRule;
  second: KeybindingConflictRule;
}

export type KeybindingConflict = KeybindingConflictBase &
  (
    | {
        /** Equal-rank ambiguity with a statically identifiable winner. */
        resolution: "ambiguous";
        winner: KeybindingConflictRule;
        loser: KeybindingConflictRule;
        reason: KeybindingConflictReason;
      }
    | {
        /** Cross-mode overlap resolved at runtime by focused scope ownership. */
        resolution: "focusResolved";
        winner?: never;
        loser?: never;
        reason?: never;
      }
  );

export interface EffectiveKeymap {
  rules: readonly KeybindingRule[];
  conflicts: readonly KeybindingConflict[];
}

const NESTED_MODE_GROUPS: readonly ReadonlySet<KeybindingMode>[] = [
  new Set(["filesPanel", "fileViewer", "codeEditor", "markdownEditor", "markdownToc"]),
  new Set(["terminalsPanel", "terminal"]),
];

export function keybindingModesMayOverlap(left: KeybindingMode, right: KeybindingMode): boolean {
  if (left === right || left === "global" || right === "global") return true;
  return NESTED_MODE_GROUPS.some((group) => group.has(left) && group.has(right));
}

function rulesMayOverlap(left: KeybindingRule, right: KeybindingRule): boolean {
  return (
    left.activation === "active" ||
    right.activation === "active" ||
    keybindingModesMayOverlap(left.mode, right.mode)
  );
}

function isKnownAction(action: string): action is ActionId {
  return ACTIONS_BY_ID.has(action as ActionId);
}

function isRecord(value: JsonValue | undefined): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(value: Record<string, JsonValue>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && actual.every((key) => keys.includes(key));
}

const ARG_VALIDATORS = {
  "session.action.openPinned": (candidate: JsonValue | undefined) =>
    isRecord(candidate) &&
    hasOnlyKeys(candidate, ["slot"]) &&
    typeof candidate.slot === "number" &&
    Number.isInteger(candidate.slot) &&
    candidate.slot >= 0 &&
    candidate.slot <= 9,
  "composer.action.acceptSuggestion": (candidate: JsonValue | undefined) =>
    isRecord(candidate) &&
    hasOnlyKeys(candidate, ["behavior"]) &&
    (candidate.behavior === "openOrAttach" || candidate.behavior === "attach"),
  "terminal.action.sendSequence": (candidate: JsonValue | undefined) =>
    isRecord(candidate) && hasOnlyKeys(candidate, ["data"]) && typeof candidate.data === "string",
} satisfies {
  [A in keyof ActionArgsById]: (candidate: JsonValue | undefined) => boolean;
};

function validArgs(action: ActionId, candidate: JsonValue | undefined): boolean {
  if (Object.hasOwn(ARG_VALIDATORS, action)) {
    return ARG_VALIDATORS[action as keyof ActionArgsById](candidate);
  }
  return candidate === undefined;
}

function jsonEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => jsonEqual(value, right[index]))
    );
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) => Object.hasOwn(rightRecord, key) && jsonEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

function immutableClone<T>(value: T): T {
  if (Array.isArray(value)) {
    return Object.freeze(value.map((child) => immutableClone(child))) as T;
  }
  if (value !== null && typeof value === "object") {
    return Object.freeze(
      Object.fromEntries(
        Object.entries(value as Record<string, unknown>).map(([key, child]) => [
          key,
          immutableClone(child),
        ]),
      ),
    ) as T;
  }
  return value;
}

function immutableRule(rule: KeybindingRule): KeybindingRule {
  return immutableClone(rule);
}

function ruleArgs(rule: KeybindingRule): JsonValue | undefined {
  return "args" in rule ? (rule.args as JsonValue | undefined) : undefined;
}

const ROUTING_FIELDS = [
  "activation",
  "when",
  "phase",
  "priority",
  "allowRepeat",
  "allowDefaultPrevented",
  "preventDefault",
  "stopPropagation",
] as const satisfies readonly (keyof KeybindingRule)[];

function sharedRoutingTemplate(candidates: readonly KeybindingRule[]): Partial<KeybindingRule> {
  if (candidates.length === 0) return {};
  const template: Record<string, unknown> = {};
  for (const field of ROUTING_FIELDS) {
    const value = candidates[0]![field];
    if (
      candidates.every((candidate) => jsonEqual(candidate[field], value)) &&
      value !== undefined
    ) {
      template[field] = value;
    }
  }
  return template as Partial<KeybindingRule>;
}

function lastOverridesById(rules: readonly UserKeybindingRule[]): readonly UserKeybindingRule[] {
  const seen = new Set<string>();
  const out: UserKeybindingRule[] = [];
  for (let index = rules.length - 1; index >= 0; index -= 1) {
    const rule = rules[index]!;
    if (seen.has(rule.id)) continue;
    seen.add(rule.id);
    out.push(rule);
  }
  return out.reverse();
}

function effectiveUserRule(
  user: UserKeybindingRule,
  defaults: readonly KeybindingRule[],
  target?: KeybindingRule,
): KeybindingRule | null {
  if (!isKnownAction(user.action) || user.sequence === null) return null;
  let sequence;
  try {
    sequence = parseKeybinding(user.sequence);
  } catch {
    return null;
  }
  const candidateArgs = Object.hasOwn(user, "args")
    ? user.args
    : target
      ? ruleArgs(target)
      : undefined;
  if (!validArgs(user.action, candidateArgs)) return null;
  const matchingTemplates = target
    ? [target]
    : defaults.filter(
        (rule) =>
          rule.action === user.action &&
          rule.mode === user.mode &&
          jsonEqual(ruleArgs(rule), candidateArgs),
      );
  return {
    ...(target ?? sharedRoutingTemplate(matchingTemplates)),
    id: user.id,
    action: user.action,
    mode: user.mode,
    sequence,
    ...(candidateArgs === undefined ? {} : { args: candidateArgs }),
    origin: "user",
  } as KeybindingRule;
}

function conflictRule(rule: KeybindingRule): KeybindingConflictRule {
  return Object.freeze({
    id: rule.id,
    action: rule.action,
    mode: rule.mode,
    origin: rule.origin === "user" ? "user" : "default",
  });
}

function collision(
  left: KeybindingRule,
  right: KeybindingRule,
): Pick<KeybindingConflict, "sequence" | "kind"> | null {
  if (left.sequence.length === 0 || right.sequence.length === 0) return null;
  const leftSequence = serializeKeybinding(left.sequence);
  const rightSequence = serializeKeybinding(right.sequence);
  if (leftSequence === rightSequence) return { sequence: leftSequence, kind: "exact" };
  const leftFirst = serializeKeybinding([left.sequence[0]!]);
  const rightFirst = serializeKeybinding([right.sequence[0]!]);
  if (leftFirst !== rightFirst) return null;
  if (left.sequence.length === 1 || right.sequence.length === 1) {
    return { sequence: leftFirst, kind: "chordPrefix" };
  }
  return null;
}

function staticWinner(
  left: KeybindingRule,
  right: KeybindingRule,
  leftIndex: number,
  rightIndex: number,
  kind: KeybindingConflict["kind"],
): { winner: KeybindingRule; loser: KeybindingRule; reason: KeybindingConflictReason } {
  if (kind === "chordPrefix") {
    const winner = left.sequence.length > 1 ? left : right;
    return { winner, loser: winner === left ? right : left, reason: "chordPrefix" };
  }
  const comparisons: readonly [number, KeybindingConflictReason][] = [
    [(left.priority ?? 0) - (right.priority ?? 0), "priority"],
    [contextSpecificity(left.when) - contextSpecificity(right.when), "specificity"],
    [Number(left.origin === "user") - Number(right.origin === "user"), "user"],
    [leftIndex - rightIndex, "later"],
  ];
  const [difference, reason] = comparisons.find(([value]) => value !== 0) ?? [-1, "later"];
  const winner = difference > 0 ? left : right;
  return { winner, loser: winner === left ? right : left, reason };
}

export function isUserKeybindingRuleUsable(
  defaults: readonly KeybindingRule[],
  user: UserKeybindingRule,
): boolean {
  const target = defaults.find((rule) => rule.id === user.id);
  if (target && (target.action !== user.action || target.mode !== user.mode)) return false;
  if (user.sequence === null) return target !== undefined;
  return effectiveUserRule(user, defaults, target) !== null;
}

export function analyzeKeybindingConflicts(
  rules: readonly KeybindingRule[],
): readonly KeybindingConflict[] {
  const conflicts: KeybindingConflict[] = [];
  for (let leftIndex = 0; leftIndex < rules.length; leftIndex += 1) {
    const left = rules[leftIndex]!;
    for (let rightIndex = leftIndex + 1; rightIndex < rules.length; rightIndex += 1) {
      const right = rules[rightIndex]!;
      if (left.origin !== "user" && right.origin !== "user") continue;
      const overlap = collision(left, right);
      if (!overlap || !rulesMayOverlap(left, right)) continue;
      if (!contextsMayOverlap(left.when, right.when)) continue;
      const base = {
        ...overlap,
        first: conflictRule(left),
        second: conflictRule(right),
      };
      const activeTie = left.activation === "active" && right.activation === "active";
      if (left.mode !== right.mode && !activeTie) {
        conflicts.push(Object.freeze({ ...base, resolution: "focusResolved" }));
      } else {
        const ranked = staticWinner(left, right, leftIndex, rightIndex, overlap.kind);
        conflicts.push(
          Object.freeze({
            ...base,
            resolution: "ambiguous",
            winner: conflictRule(ranked.winner),
            loser: conflictRule(ranked.loser),
            reason: ranked.reason,
          }),
        );
      }
    }
  }
  return Object.freeze(conflicts);
}

function sameBinding(left: KeybindingRule, right: KeybindingRule): boolean {
  return (
    serializeKeybinding(left.sequence) === serializeKeybinding(right.sequence) &&
    jsonEqual(ruleArgs(left), ruleArgs(right))
  );
}

/** Merge override-only preferences over the current product defaults. */
export function resolveEffectiveKeymap(
  defaults: readonly KeybindingRule[],
  userRules: readonly UserKeybindingRule[],
): EffectiveKeymap {
  const overrides = lastOverridesById(userRules);
  const overridesById = new Map(overrides.map((rule) => [rule.id, rule]));
  const defaultIds = new Set(defaults.map((rule) => rule.id));
  const rules: KeybindingRule[] = [];

  for (const defaultRule of defaults) {
    const user = overridesById.get(defaultRule.id);
    if (!user || user.action !== defaultRule.action || user.mode !== defaultRule.mode) {
      rules.push(immutableRule({ ...defaultRule, origin: "default" } as KeybindingRule));
      continue;
    }
    if (user.sequence === null) continue;
    const replacement = effectiveUserRule(user, defaults, defaultRule);
    rules.push(
      replacement && !sameBinding(replacement, defaultRule)
        ? immutableRule(replacement)
        : immutableRule({ ...defaultRule, origin: "default" } as KeybindingRule),
    );
  }

  for (const user of overrides) {
    if (defaultIds.has(user.id)) continue;
    const alternate = effectiveUserRule(user, defaults);
    if (alternate) rules.push(immutableRule(alternate));
  }

  const frozenRules = Object.freeze(rules);
  return Object.freeze({ rules: frozenRules, conflicts: analyzeKeybindingConflicts(frozenRules) });
}
