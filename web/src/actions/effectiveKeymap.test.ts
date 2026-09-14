import { describe, expect, it } from "vitest";
import { not, when } from "./context";
import { DEFAULT_KEYBINDINGS } from "./defaultKeybindings";
import {
  analyzeKeybindingConflicts,
  isUserKeybindingRuleUsable,
  keybindingModesMayOverlap,
  resolveEffectiveKeymap,
} from "./effectiveKeymap";
import type { UserKeybindingRule } from "./keybindingPreferences";
import { parseKeybinding, serializeKeybinding } from "./keybindingParser";
import type { ActionId, KeybindingMode, KeybindingRule } from "./types";

function defaultRule(
  id: string,
  action: ActionId,
  sequence: string,
  mode: KeybindingMode = "global",
  patch: Partial<KeybindingRule> = {},
): KeybindingRule {
  return {
    id,
    action,
    sequence: parseKeybinding(sequence),
    mode,
    preventDefault: true,
    ...patch,
  } as KeybindingRule;
}

function user(
  id: string,
  action: string,
  sequence: string | null,
  mode: KeybindingMode = "global",
  args?: UserKeybindingRule["args"],
): UserKeybindingRule {
  return { id, action, sequence, mode, ...(args === undefined ? {} : { args }) };
}

function serialized(rules: readonly KeybindingRule[]): string[] {
  return rules.map((rule) => `${rule.id}:${serializeKeybinding(rule.sequence)}`);
}

describe("effective keymap", () => {
  it("preserves default behavior and metadata when no overrides exist", () => {
    const defaults = [
      defaultRule("session.new", "session.action.new", "mod+n", "global", {
        phase: "capture",
      }),
    ];
    const effective = resolveEffectiveKeymap(defaults, []);
    expect(serialized(effective.rules)).toEqual(["session.new:mod+n"]);
    expect(effective.rules[0]).toMatchObject({ phase: "capture", origin: "default" });
    expect(effective.conflicts).toEqual([]);
  });

  it("replaces a targeted default while preserving its routing metadata", () => {
    const defaults = [
      defaultRule("session.new", "session.action.new", "mod+n", "global", {
        phase: "capture",
        allowRepeat: true,
      }),
    ];
    const effective = resolveEffectiveKeymap(defaults, [
      user("session.new", "session.action.new", "ctrl+shift+n"),
    ]);
    expect(serialized(effective.rules)).toEqual(["session.new:ctrl+shift+n"]);
    expect(effective.rules[0]).toMatchObject({
      phase: "capture",
      allowRepeat: true,
      origin: "user",
    });
  });

  it("adds alternate bindings without removing the default", () => {
    const defaults = [defaultRule("session.new", "session.action.new", "mod+n")];
    const effective = resolveEffectiveKeymap(defaults, [
      user("user.session.new.alternate", "session.action.new", "ctrl+shift+n"),
    ]);
    expect(serialized(effective.rules)).toEqual([
      "session.new:mod+n",
      "user.session.new.alternate:ctrl+shift+n",
    ]);
  });

  it("explicitly unbinds only the matching id/action/mode tuple", () => {
    const defaults = [
      defaultRule("shared-id", "session.action.new", "mod+n"),
      defaultRule("other", "workbench.action.showCommands", "mod+k"),
    ];
    expect(
      serialized(
        resolveEffectiveKeymap(defaults, [user("shared-id", "session.action.new", null)]).rules,
      ),
    ).toEqual(["other:mod+k"]);
    expect(
      serialized(
        resolveEffectiveKeymap(defaults, [user("shared-id", "future.action", null)]).rules,
      ),
    ).toEqual(["shared-id:mod+n", "other:mod+k"]);
  });

  it("resetting an override reveals new current defaults", () => {
    const override = user("session.new", "session.action.new", "ctrl+shift+n");
    const defaults = [defaultRule("session.new", "session.action.new", "mod+n")];
    expect(serialized(resolveEffectiveKeymap(defaults, [override]).rules)).toEqual([
      "session.new:ctrl+shift+n",
    ]);

    const nextDefaults = [
      ...defaults,
      defaultRule("session.new.secondary", "session.action.new", "mod+shift+n"),
    ];
    expect(serialized(resolveEffectiveKeymap(nextDefaults, [override]).rules)).toEqual([
      "session.new:ctrl+shift+n",
      "session.new.secondary:mod+shift+n",
    ]);
    expect(serialized(resolveEffectiveKeymap(nextDefaults, []).rules)).toEqual([
      "session.new:mod+n",
      "session.new.secondary:mod+shift+n",
    ]);
  });

  it("keeps unknown actions dormant and activates valid typed arguments", () => {
    const defaults = [
      defaultRule(
        "terminal.shiftEnter",
        "terminal.action.sendSequence",
        "shift+enter",
        "terminal",
        {
          args: { data: "old" },
        },
      ),
    ];
    const effective = resolveEffectiveKeymap(defaults, [
      user("future", "future.action.run", "mod+j"),
      user("terminal.shiftEnter", "terminal.action.sendSequence", "ctrl+enter", "terminal", {
        data: "new",
      }),
    ]);
    expect(serialized(effective.rules)).toEqual(["terminal.shiftEnter:ctrl+Enter"]);
    expect(effective.rules[0]).toMatchObject({ args: { data: "new" }, origin: "user" });
  });

  it("reports overlaps but allows the same key in disjoint modes and contexts", () => {
    expect(keybindingModesMayOverlap("global", "composer")).toBe(true);
    expect(keybindingModesMayOverlap("fileViewer", "markdownToc")).toBe(true);
    expect(keybindingModesMayOverlap("filesPanel", "fileViewer")).toBe(true);
    expect(keybindingModesMayOverlap("composer", "terminal")).toBe(false);

    const rules = [
      defaultRule("global", "session.action.new", "escape"),
      {
        ...defaultRule("composer", "composer.action.stop", "escape", "composer"),
        origin: "user" as const,
        when: when("composerStreaming"),
      },
      {
        ...defaultRule("composer-idle", "composer.action.send", "escape", "composer"),
        origin: "user" as const,
        when: not(when("composerStreaming")),
      },
      {
        ...defaultRule("terminal", "terminal.action.sendSequence", "escape", "terminal", {
          args: { data: "x" },
        }),
        origin: "user" as const,
      },
    ];
    const conflicts = analyzeKeybindingConflicts(rules);
    expect(conflicts.map(({ first, second }) => [first.id, second.id])).toEqual([
      ["global", "composer"],
      ["global", "composer-idle"],
      ["global", "terminal"],
    ]);
  });

  it("keeps a default when a structurally valid override has unusable args", () => {
    const defaults = [
      defaultRule("terminal", "terminal.action.sendSequence", "shift+enter", "terminal", {
        args: { data: "default" },
      }),
    ];
    const invalid = user("terminal", "terminal.action.sendSequence", "ctrl+enter", "terminal", 5);
    const effective = resolveEffectiveKeymap(defaults, [invalid]);
    expect(serialized(effective.rules)).toEqual(["terminal:shift+Enter"]);
    expect(effective.rules[0]).toMatchObject({ args: { data: "default" }, origin: "default" });
    expect(isUserKeybindingRuleUsable(defaults, invalid)).toBe(false);
  });

  it("does not coerce explicit null args back to target defaults", () => {
    const defaults = [
      defaultRule("terminal", "terminal.action.sendSequence", "shift+enter", "terminal", {
        args: { data: "default" },
      }),
    ];
    const invalid = user(
      "terminal",
      "terminal.action.sendSequence",
      "ctrl+enter",
      "terminal",
      null,
    );
    expect(serialized(resolveEffectiveKeymap(defaults, [invalid]).rules)).toEqual([
      "terminal:shift+Enter",
    ]);
  });

  it("reports active-mode conflicts with the static winner", () => {
    const rules = [
      {
        ...defaultRule("files", "panel.action.closeFiles", "escape", "filesPanel", {
          activation: "active",
        }),
        origin: "user" as const,
      },
      defaultRule("terminals", "panel.action.closeTerminals", "escape", "terminalsPanel", {
        activation: "active",
      }),
    ];
    expect(analyzeKeybindingConflicts(rules)).toEqual([
      expect.objectContaining({
        kind: "exact",
        first: expect.objectContaining({ id: "files" }),
        second: expect.objectContaining({ id: "terminals" }),
        resolution: "ambiguous",
        winner: expect.objectContaining({ id: "files" }),
        reason: "user",
      }),
    ]);
  });

  it("detects platform-overlapping mod and legacy primary bindings", () => {
    const conflicts = analyzeKeybindingConflicts([
      defaultRule("legacy", "workbench.action.showCommands", "primary+k"),
      { ...defaultRule("user", "session.action.new", "mod+k"), origin: "user" },
    ]);
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]).toMatchObject({ kind: "exact", resolution: "ambiguous" });
  });

  it("detects logical keys that overlap physical-code defaults", () => {
    const conflicts = analyzeKeybindingConflicts([
      defaultRule("physical", "composer.action.toggleDictation", "primary+alt+[KeyV]"),
      { ...defaultRule("logical", "session.action.new", "mod+alt+v"), origin: "user" },
    ]);
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0]).toMatchObject({ kind: "exact", resolution: "ambiguous" });
  });

  it("marks the same-mode user binding as the static winner", () => {
    const conflicts = analyzeKeybindingConflicts([
      defaultRule("default", "workbench.action.showCommands", "mod+k"),
      {
        ...defaultRule("user", "session.action.new", "mod+k"),
        origin: "user",
      },
    ]);
    expect(conflicts).toEqual([
      expect.objectContaining({
        resolution: "ambiguous",
        winner: expect.objectContaining({ id: "user" }),
        loser: expect.objectContaining({ id: "default" }),
        reason: "user",
      }),
    ]);
  });

  it("builds alternates from shared routing policy rather than one platform default", () => {
    const alternate = user("user.pinned.3", "session.action.openPinned", "ctrl+alt+f3", "global", {
      slot: 2,
    });
    const effective = resolveEffectiveKeymap(DEFAULT_KEYBINDINGS, [alternate]);
    const rule = effective.rules.find((candidate) => candidate.id === alternate.id)!;
    expect(rule.when).toBeUndefined();
    expect(rule.allowDefaultPrevented).toBe(true);
    expect(rule.args).toEqual({ slot: 2 });
  });

  it("treats ids consistently and preserves replacement positions", () => {
    const defaults = [
      defaultRule("first", "panel.action.closeFiles", "escape", "filesPanel", {
        activation: "active",
      }),
      defaultRule("second", "panel.action.closeTerminals", "escape", "terminalsPanel", {
        activation: "active",
      }),
    ];
    const mismatched = user("first", "panel.action.closeFiles", "ctrl+x", "composer");
    expect(serialized(resolveEffectiveKeymap(defaults, [mismatched]).rules)).toEqual([
      "first:Escape",
      "second:Escape",
    ]);
    expect(isUserKeybindingRuleUsable(defaults, mismatched)).toBe(false);

    const replaced = resolveEffectiveKeymap(defaults, [
      user("first", "panel.action.closeFiles", "ctrl+1", "filesPanel"),
      user("second", "panel.action.closeTerminals", "ctrl+2", "terminalsPanel"),
    ]);
    expect(serialized(replaced.rules)).toEqual(["first:ctrl+1", "second:ctrl+2"]);
  });

  it("rejects out-of-range or extra typed args", () => {
    const invalidSlot = user(
      "session.openPinned.native.1",
      "session.action.openPinned",
      "ctrl+1",
      "global",
      { slot: 99 },
    );
    const extra = user(
      "terminal.sendShiftEnter",
      "terminal.action.sendSequence",
      "ctrl+enter",
      "terminal",
      { data: "x", extra: true },
    );
    expect(isUserKeybindingRuleUsable(DEFAULT_KEYBINDINGS, invalidSlot)).toBe(false);
    expect(isUserKeybindingRuleUsable(DEFAULT_KEYBINDINGS, extra)).toBe(false);
  });

  it("deep-clones and freezes effective rules without freezing source defaults", () => {
    const source = defaultRule(
      "terminal",
      "terminal.action.sendSequence",
      "shift+enter",
      "terminal",
      {
        args: { data: "x" },
      },
    );
    const effective = resolveEffectiveKeymap([source], []);
    const rule = effective.rules[0]!;
    expect(rule.sequence).not.toBe(source.sequence);
    expect(rule.args).not.toBe(source.args);
    expect(Object.isFrozen(rule)).toBe(true);
    expect(Object.isFrozen(rule.sequence)).toBe(true);
    expect(Object.isFrozen(rule.sequence[0])).toBe(true);
    expect(Object.isFrozen(rule.args)).toBe(true);
    expect(Object.isFrozen(source.sequence)).toBe(false);
  });

  it("treats an identical recorded default as an ordering-neutral no-op", () => {
    const defaults = [
      defaultRule("files", "panel.action.closeFiles", "escape", "filesPanel", {
        activation: "active",
      }),
      defaultRule("toc", "panel.action.closeMarkdownToc", "escape", "markdownToc", {
        activation: "active",
      }),
    ];
    const effective = resolveEffectiveKeymap(defaults, [
      user("files", "panel.action.closeFiles", "escape", "filesPanel"),
    ]);
    expect(effective.rules.map((rule) => [rule.id, rule.origin])).toEqual([
      ["files", "default"],
      ["toc", "default"],
    ]);
    expect(effective.conflicts).toEqual([]);
  });

  it("inherits action routing policy without key-specific when conditions", () => {
    const send = resolveEffectiveKeymap(DEFAULT_KEYBINDINGS, [
      user("user.send", "composer.action.send", "mod+m", "composer"),
    ]).rules.find((rule) => rule.id === "user.send")!;
    expect(send.when).toBeUndefined();

    const terminal = resolveEffectiveKeymap(DEFAULT_KEYBINDINGS, [
      user("user.terminal", "terminal.action.sendSequence", "mod+enter", "terminal", {
        data: "custom",
      }),
    ]).rules.find((rule) => rule.id === "user.terminal")!;
    expect(terminal).toMatchObject({ phase: "capture", stopPropagation: true });
  });

  it("defaults cross-action bindings in open-surface modes to active", () => {
    const defaults = [defaultRule("session.new", "session.action.new", "mod+n")];
    const effective = resolveEffectiveKeymap(defaults, [
      user("user.session.files", "session.action.new", "mod+j", "filesPanel"),
    ]);
    expect(effective.rules.find((rule) => rule.id === "user.session.files")).toMatchObject({
      mode: "filesPanel",
      activation: "active",
    });
  });

  it("uses the last hand-edited duplicate override deterministically", () => {
    const defaults = [defaultRule("session.new", "session.action.new", "mod+n")];
    const effective = resolveEffectiveKeymap(defaults, [
      user("session.new", "session.action.new", "ctrl+n"),
      user("session.new", "session.action.new", "ctrl+shift+n"),
    ]);
    expect(serialized(effective.rules)).toEqual(["session.new:ctrl+shift+n"]);
  });
});
