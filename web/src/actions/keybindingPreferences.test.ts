import { describe, expect, it, vi } from "vitest";
import {
  KEYBINDINGS_STORAGE_KEY,
  MAX_USER_KEYBINDINGS,
  parseUserKeybindingPreferences,
  readUserKeybindings,
  writeUserKeybindings,
  type UserKeybindingRule,
} from "./keybindingPreferences";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    values,
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => values.set(key, value)),
    removeItem: vi.fn((key: string) => values.delete(key)),
  };
}

const known: UserKeybindingRule = {
  id: "session.new",
  action: "session.action.new",
  sequence: "ctrl+shift+n",
  mode: "global",
};

function stored(rules: unknown): string {
  return JSON.stringify(rules);
}

describe("keybinding preferences", () => {
  it("loads ordered overrides, canonicalizes sequences, and preserves unknown actions", () => {
    const rules = parseUserKeybindingPreferences(
      stored([
        { ...known, sequence: "SHIFT+CTRL+N" },
        { id: "future", action: "future.action.run", sequence: null, mode: "global" },
      ]),
    );
    expect(rules).toEqual([
      known,
      { id: "future", action: "future.action.run", sequence: null, mode: "global" },
    ]);
    expect(Object.isFrozen(rules)).toBe(true);
  });

  it("reads the pre-release v1 envelope without losing overrides", () => {
    expect(parseUserKeybindingPreferences(JSON.stringify({ version: 1, rules: [known] }))).toEqual([
      known,
    ]);
  });

  it.each([null, "{", "[]", "{}", stored("not-an-array")])(
    "returns an empty list for corrupt or incompatible input %#",
    (raw) => {
      expect(parseUserKeybindingPreferences(raw)).toEqual([]);
    },
  );

  it("drops malformed records independently", () => {
    const rules = parseUserKeybindingPreferences(
      stored([
        known,
        { ...known, id: "bad-mode", mode: "future" },
        { ...known, id: "bad-key", sequence: "ctrl+" },
        { ...known, id: "bad-action", action: "" },
      ]),
    );
    expect(rules).toEqual([known]);
  });

  it("rejects an invalid write without narrowing existing storage", () => {
    const storage = memoryStorage();
    storage.values.set(KEYBINDINGS_STORAGE_KEY, stored([known]));
    const invalid = { ...known, args: { nested: undefined } } as unknown as UserKeybindingRule;
    expect(writeUserKeybindings([invalid], storage)).toBe(false);
    expect(readUserKeybindings(storage)).toEqual([known]);
  });

  it("caps hostile stored arrays and rejects oversized writes", () => {
    const storage = memoryStorage();
    const rules = Array.from({ length: MAX_USER_KEYBINDINGS + 1 }, (_, index) => ({
      ...known,
      id: `rule-${index}`,
    }));
    expect(parseUserKeybindingPreferences(stored(rules))).toHaveLength(MAX_USER_KEYBINDINGS);
    expect(writeUserKeybindings(rules, storage)).toBe(false);
    expect(storage.setItem).not.toHaveBeenCalled();
  });

  it("writes override-only data without filtering unknown action ids", () => {
    const storage = memoryStorage();
    const future = { ...known, id: "future", action: "future.action.run" };
    expect(writeUserKeybindings([known, future], storage)).toBe(true);
    expect(storage.setItem).toHaveBeenCalledWith(KEYBINDINGS_STORAGE_KEY, expect.any(String));
    expect(readUserKeybindings(storage)).toEqual([known, future]);
  });

  it("removes storage when all overrides are reset", () => {
    const storage = memoryStorage();
    storage.values.set(KEYBINDINGS_STORAGE_KEY, stored([known]));
    expect(writeUserKeybindings([], storage)).toBe(true);
    expect(storage.removeItem).toHaveBeenCalledWith(KEYBINDINGS_STORAGE_KEY);
  });

  it("fails closed when storage access is unavailable", () => {
    const storage = {
      getItem: vi.fn(() => {
        throw new Error("disabled");
      }),
      setItem: vi.fn(() => {
        throw new Error("quota");
      }),
      removeItem: vi.fn(() => {
        throw new Error("disabled");
      }),
    };
    expect(readUserKeybindings(storage)).toEqual([]);
    expect(writeUserKeybindings([known], storage)).toBe(false);
    expect(writeUserKeybindings([], storage)).toBe(false);
    expect(readUserKeybindings(null)).toEqual([]);
  });
});
