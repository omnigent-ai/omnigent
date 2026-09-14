import { describe, expect, it } from "vitest";
import { EMPTY_ACTION_CONTEXT, when } from "./context";
import { parseKeybinding } from "./keybindingParser";
import {
  keyStrokeMatchesEvent,
  matchingKeybindingRules,
  ruleModeMatches,
  type KeybindingEnvironment,
} from "./keybindingResolver";
import type { KeybindingRule } from "./types";

function event(key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
}

const environment: KeybindingEnvironment = {
  context: EMPTY_ACTION_CONTEXT,
  focusedModes: new Set(["global"]),
  activeModes: new Set(["global"]),
};

function testRule(
  id: string,
  sequence: string,
  patch: Partial<KeybindingRule> = {},
): KeybindingRule {
  return {
    id,
    action: "session.action.new",
    sequence: parseKeybinding(sequence),
    mode: "global",
    ...patch,
  } as KeybindingRule;
}

describe("keybinding resolver", () => {
  it("distinguishes platform mod from legacy either-primary semantics", () => {
    const platform = parseKeybinding("mod+k")[0]!;
    const primary = parseKeybinding("primary+k")[0]!;
    expect(keyStrokeMatchesEvent(platform, event("k", { metaKey: true }), true)).toBe(true);
    expect(keyStrokeMatchesEvent(platform, event("k", { ctrlKey: true }), true)).toBe(false);
    expect(keyStrokeMatchesEvent(primary, event("k", { ctrlKey: true }), true)).toBe(true);
    expect(keyStrokeMatchesEvent(primary, event("k", { metaKey: true }), false)).toBe(true);
  });

  it("matches physical codes independently of a modified logical key", () => {
    const stroke = parseKeybinding("primary+alt+[BracketLeft]")[0]!;
    expect(
      keyStrokeMatchesEvent(
        stroke,
        event("“", { code: "BracketLeft", metaKey: true, altKey: true }),
        true,
      ),
    ).toBe(true);
  });

  it("requires exact Alt and Shift modifiers", () => {
    const stroke = parseKeybinding("primary+k")[0]!;
    expect(
      keyStrokeMatchesEvent(stroke, event("k", { ctrlKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it("matches focused modes through ancestry and state-active modes separately", () => {
    const focused = testRule("focused", "escape", { mode: "fileViewer" });
    const active = testRule("active", "escape", {
      mode: "filesPanel",
      activation: "active",
    });
    expect(
      ruleModeMatches(
        focused,
        new Set(["global", "markdownToc", "fileViewer"]),
        new Set(["global", "fileViewer"]),
      ),
    ).toBe(true);
    expect(
      ruleModeMatches(active, new Set(["global", "composer"]), new Set(["global", "filesPanel"])),
    ).toBe(true);
  });

  it("filters by phase, context, mode, and repeat policy", () => {
    const rules = [
      testRule("disabled-context", "primary+k", {
        when: when("terminalFocus"),
      }),
      testRule("repeat-blocked", "primary+k"),
      testRule("repeat-allowed", "primary+k", { allowRepeat: true, priority: 2 }),
      testRule("capture", "primary+k", { phase: "capture", priority: 3 }),
    ];
    const repeated = event("k", { ctrlKey: true, repeat: true });
    expect(
      matchingKeybindingRules(rules, repeated, 0, "bubble", environment).map((r) => r.id),
    ).toEqual(["repeat-allowed"]);
    expect(matchingKeybindingRules(rules, repeated, 0, "capture", environment)).toEqual([]);
  });

  it("ranks focused and global ownership ahead of active background modes", () => {
    const rules = [
      testRule("active-file", "escape", {
        mode: "fileViewer",
        activation: "active",
        priority: 100,
      }),
      testRule("global", "escape"),
      testRule("focused-composer", "escape", { mode: "composer" }),
    ];
    expect(
      matchingKeybindingRules(rules, event("Escape"), 0, "bubble", {
        ...environment,
        focusedModes: new Set(["global", "composer"]),
        activeModes: new Set(["global", "composer", "fileViewer"]),
        focusedModeRanks: new Map([
          ["global", 0],
          ["composer", 1],
        ]),
      }).map((rule) => rule.id),
    ).toEqual(["focused-composer", "global", "active-file"]);
  });

  it("ranks a focused active mode ahead of global and background active rules", () => {
    const rules = [
      testRule("focused-panel", "escape", {
        mode: "terminalsPanel",
        activation: "active",
      }),
      testRule("global-active", "escape", { activation: "active" }),
      testRule("background-panel", "escape", {
        mode: "filesPanel",
        activation: "active",
      }),
    ];
    expect(
      matchingKeybindingRules(rules, event("Escape"), 0, "bubble", {
        ...environment,
        focusedModes: new Set(["global", "terminalsPanel"]),
        activeModes: new Set(["global", "filesPanel", "terminalsPanel"]),
        focusedModeRanks: new Map([
          ["global", 0],
          ["terminalsPanel", 1],
        ]),
      }).map((rule) => rule.id),
    ).toEqual(["focused-panel", "global-active", "background-panel"]);
  });

  it("prefers a user rule over an otherwise-equal later default", () => {
    const rules = [
      testRule("user", "primary+k", { origin: "user" }),
      testRule("default", "primary+k", { origin: "default" }),
    ];
    expect(
      matchingKeybindingRules(rules, event("k", { ctrlKey: true }), 0, "bubble", environment).map(
        (rule) => rule.id,
      ),
    ).toEqual(["user", "default"]);
  });

  it("sorts by priority, context specificity, then later rule", () => {
    const context = { ...EMPTY_ACTION_CONTEXT, terminalFocus: true };
    const rules = [
      testRule("early", "primary+k"),
      testRule("specific", "primary+k", { when: when("terminalFocus") }),
      testRule("priority", "primary+k", { priority: 10 }),
      testRule("later", "primary+k"),
    ];
    expect(
      matchingKeybindingRules(rules, event("k", { ctrlKey: true }), 0, "bubble", {
        ...environment,
        context,
      }).map((rule) => rule.id),
    ).toEqual(["priority", "specific", "later", "early"]);
  });
});
