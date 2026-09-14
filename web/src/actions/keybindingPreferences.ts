import { parseKeybinding, serializeKeybinding } from "./keybindingParser";
import {
  KEYBINDING_MODES,
  type ActionArgs,
  type ActionId,
  type JsonValue,
  type KeybindingMode,
} from "./types";

/** The version lives in the key, so a future schema writes a different key. */
export const KEYBINDINGS_STORAGE_KEY = "omnigent:keybindings:v1";
export const MAX_USER_KEYBINDINGS = 500;

export interface UserKeybindingRule {
  id: string;
  action: string;
  sequence: string | null;
  mode: KeybindingMode;
  args?: JsonValue;
}

type KnownRuleArgs<A extends ActionId> =
  undefined extends ActionArgs<A> ? { args?: undefined } : { args: ActionArgs<A> };

/** Catalog-backed mutation input; persisted reads remain open to future action strings. */
export type KnownUserKeybindingRule<A extends ActionId = ActionId> = A extends ActionId
  ? | ({
        id: string;
        action: A;
        sequence: string;
        mode: KeybindingMode;
      } & KnownRuleArgs<A>)
    | {
        id: string;
        action: A;
        sequence: null;
        mode: KeybindingMode;
        args?: undefined;
      }
  : never;

type KeybindingStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const MODES = new Set<string>(KEYBINDING_MODES);
const EMPTY_RULES: readonly UserKeybindingRule[] = Object.freeze([]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return isRecord(value) && Object.values(value).every(isJsonValue);
}

function immutableJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return Object.freeze(value.map(immutableJson)) as unknown as JsonValue;
  if (value !== null && typeof value === "object") {
    return Object.freeze(
      Object.fromEntries(Object.entries(value).map(([key, child]) => [key, immutableJson(child)])),
    ) as JsonValue;
  }
  return value;
}

function normalizeSequence(value: unknown): string | null | undefined {
  if (value === null) return null;
  if (typeof value !== "string" || value.length === 0) return undefined;
  try {
    return serializeKeybinding(parseKeybinding(value));
  } catch {
    return undefined;
  }
}

export function normalizeUserKeybindingRule(value: unknown): UserKeybindingRule | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string" || value.id.length === 0) return null;
  if (typeof value.action !== "string" || value.action.length === 0) return null;
  if (typeof value.mode !== "string" || !MODES.has(value.mode)) return null;
  const sequence = normalizeSequence(value.sequence);
  if (sequence === undefined) return null;
  if (Object.hasOwn(value, "args") && !isJsonValue(value.args)) return null;
  const rule: UserKeybindingRule = {
    id: value.id,
    action: value.action,
    sequence,
    mode: value.mode as KeybindingMode,
  };
  if (Object.hasOwn(value, "args")) rule.args = immutableJson(value.args as JsonValue);
  return Object.freeze(rule);
}

/** Invalid rows are dropped on read and are not preserved by later mutations. */
export function parseUserKeybindingPreferences(raw: string | null): readonly UserKeybindingRule[] {
  if (!raw) return EMPTY_RULES;
  try {
    const value: unknown = JSON.parse(raw);
    // Accept the short-lived pre-release envelope while always writing the v1-key-owned array.
    const candidates = Array.isArray(value)
      ? value
      : isRecord(value) && value.version === 1 && Array.isArray(value.rules)
        ? value.rules
        : null;
    if (!candidates) return EMPTY_RULES;
    return Object.freeze(
      candidates.slice(0, MAX_USER_KEYBINDINGS).flatMap((candidate) => {
        const rule = normalizeUserKeybindingRule(candidate);
        return rule ? [rule] : [];
      }),
    );
  } catch {
    return EMPTY_RULES;
  }
}

function browserStorage(): KeybindingStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function readUserKeybindings(
  storage: KeybindingStorage | null = browserStorage(),
): readonly UserKeybindingRule[] {
  if (!storage) return EMPTY_RULES;
  try {
    return parseUserKeybindingPreferences(storage.getItem(KEYBINDINGS_STORAGE_KEY));
  } catch {
    return EMPTY_RULES;
  }
}

/**
 * Best-effort strict write. Unknown actions survive, but an invalid input row
 * rejects the write so a read-modify-write cannot narrow or erase the keymap.
 */
export function writeUserKeybindings(
  rules: readonly UserKeybindingRule[],
  storage: KeybindingStorage | null = browserStorage(),
): boolean {
  if (!storage || rules.length > MAX_USER_KEYBINDINGS) return false;
  const normalized: UserKeybindingRule[] = [];
  for (const candidate of rules) {
    const rule = normalizeUserKeybindingRule(candidate);
    if (!rule) return false;
    normalized.push(rule);
  }
  try {
    if (rules.length === 0) {
      storage.removeItem(KEYBINDINGS_STORAGE_KEY);
    } else {
      storage.setItem(KEYBINDINGS_STORAGE_KEY, JSON.stringify(normalized));
    }
    return true;
  } catch {
    return false;
  }
}
