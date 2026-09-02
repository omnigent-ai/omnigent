import { describe, expect, it } from "vitest";
import { ACTION_CATALOG, getActionDefinition } from "./catalog";
import { contextsMayOverlap, EMPTY_ACTION_CONTEXT, evaluateContext } from "./context";
import { DEFAULT_KEYBINDINGS } from "./defaultKeybindings";
import { parseKeybinding, serializeKeybinding } from "./keybindingParser";
import { ACTION_IDS } from "./types";

describe("default keybindings", () => {
  it("has exactly one catalog entry for every stable action ID", () => {
    const catalogIds = ACTION_CATALOG.map((action) => action.id);
    expect(new Set(catalogIds).size).toBe(catalogIds.length);
    expect(new Set(catalogIds)).toEqual(new Set(ACTION_IDS));
    for (const id of ACTION_IDS) expect(getActionDefinition(id).id).toBe(id);
  });

  it("uses unique rule IDs and known action IDs", () => {
    const ruleIds = DEFAULT_KEYBINDINGS.map((rule) => rule.id);
    expect(new Set(ruleIds).size).toBe(ruleIds.length);
    const actionIds = new Set(ACTION_IDS);
    for (const rule of DEFAULT_KEYBINDINGS) expect(actionIds.has(rule.action)).toBe(true);
  });

  it("round-trips every default sequence through its persisted form", () => {
    for (const rule of DEFAULT_KEYBINDINGS) {
      expect(parseKeybinding(serializeKeybinding(rule.sequence))).toEqual(rule.sequence);
    }
  });

  it("has no unresolved same-precedence binding collisions", () => {
    for (let leftIndex = 0; leftIndex < DEFAULT_KEYBINDINGS.length; leftIndex += 1) {
      const left = DEFAULT_KEYBINDINGS[leftIndex]!;
      for (
        let rightIndex = leftIndex + 1;
        rightIndex < DEFAULT_KEYBINDINGS.length;
        rightIndex += 1
      ) {
        const right = DEFAULT_KEYBINDINGS[rightIndex]!;
        if (serializeKeybinding(left.sequence) !== serializeKeybinding(right.sequence)) continue;
        if (left.mode !== right.mode) continue;
        if ((left.phase ?? "bubble") !== (right.phase ?? "bubble")) continue;
        if ((left.activation ?? "focused") !== (right.activation ?? "focused")) continue;
        if ((left.priority ?? 0) !== (right.priority ?? 0)) continue;
        expect(
          contextsMayOverlap(left.when, right.when),
          `${left.id} conflicts with ${right.id}`,
        ).toBe(false);
      }
    }
  });

  it("defines browser and native pinned slots through one typed action", () => {
    const pinned = DEFAULT_KEYBINDINGS.filter(
      (rule) => rule.action === "session.action.openPinned",
    );
    expect(pinned).toHaveLength(20);
    expect(
      pinned.filter((rule) => serializeKeybinding(rule.sequence) === "primary+1"),
    ).toHaveLength(1);
    expect(
      pinned.filter((rule) => serializeKeybinding(rule.sequence) === "primary+alt+[Digit1]"),
    ).toHaveLength(1);
    expect(new Set(pinned.map((rule) => JSON.stringify(rule.args))).size).toBe(10);
  });

  it("keeps mention Enter and Tab semantics in typed action arguments", () => {
    const suggestions = DEFAULT_KEYBINDINGS.filter(
      (rule) => rule.action === "composer.action.acceptSuggestion",
    );
    expect(suggestions.find((rule) => rule.id.endsWith(".tab"))?.args).toEqual({
      behavior: "attach",
    });
    expect(suggestions.find((rule) => rule.id.endsWith(".enter"))?.args).toEqual({
      behavior: "openOrAttach",
    });
  });

  it("opts navigation actions into legacy auto-repeat behavior", () => {
    const repeatedActions = new Set(
      DEFAULT_KEYBINDINGS.filter((rule) => rule.allowRepeat).map((rule) => rule.action),
    );
    expect(repeatedActions).toEqual(
      new Set([
        "session.action.openPrevious",
        "session.action.openNext",
        "session.action.openPinned",
        "chat.action.openPreviousMessage",
        "chat.action.openNextMessage",
        "composer.action.selectPreviousSuggestion",
        "composer.action.selectNextSuggestion",
        "composer.action.recallPrevious",
        "composer.action.recallNext",
        "file.action.openPreviousChanged",
        "file.action.openNextChanged",
        "terminal.action.sendSequence",
      ]),
    );
  });

  it("pins legacy globals that intentionally run after preventDefault", () => {
    const actions = new Set(
      DEFAULT_KEYBINDINGS.filter((rule) => rule.allowDefaultPrevented).map((rule) => rule.action),
    );
    expect(actions).toEqual(
      new Set([
        "session.action.new",
        "session.action.openPinned",
        "workbench.action.openKeyboardShortcuts",
        "workbench.action.toggleConversationsSidebar",
        "workbench.action.toggleWorkspaceSidebar",
        "composer.action.toggleDictation",
      ]),
    );
  });

  it("does not repeat approval or send actions", () => {
    const guarded = DEFAULT_KEYBINDINGS.filter(
      (rule) =>
        rule.action === "chat.action.acceptApproval" || rule.action === "composer.action.send",
    );
    expect(guarded.length).toBeGreaterThan(0);
    expect(guarded.every((rule) => rule.allowRepeat !== true)).toBe(true);
  });

  it("marks preemptive actions as capture-phase rules", () => {
    const captureActions = new Set(
      DEFAULT_KEYBINDINGS.filter((rule) => rule.phase === "capture").map((rule) => rule.action),
    );
    expect(captureActions).toEqual(
      new Set([
        "workbench.action.showCommands",
        "chat.action.acceptApproval",
        "composer.action.commitDictation",
        "composer.action.cancelDictation",
        "file.action.find",
        "file.action.save",
        "terminal.action.sendSequence",
      ]),
    );
  });

  it("models file rules as active while keeping commands out of unrelated inputs", () => {
    const fileRules = DEFAULT_KEYBINDINGS.filter((rule) => rule.action.startsWith("file.action"));
    expect(fileRules.every((rule) => rule.activation === "active")).toBe(true);
    const find = fileRules.find((rule) => rule.action === "file.action.find")!;
    expect(evaluateContext(find.when, { ...EMPTY_ACTION_CONTEXT, inputFocus: true })).toBe(false);
    expect(
      evaluateContext(find.when, {
        ...EMPTY_ACTION_CONTEXT,
        inputFocus: true,
        monacoFocus: true,
      }),
    ).toBe(true);
    expect(
      evaluateContext(find.when, {
        ...EMPTY_ACTION_CONTEXT,
        inputFocus: true,
        markdownEditorFocus: true,
      }),
    ).toBe(true);
  });

  it("models open panels as active rather than focus-only modes", () => {
    const panelRules = DEFAULT_KEYBINDINGS.filter((rule) => rule.action.startsWith("panel.action"));
    expect(panelRules).toHaveLength(4);
    expect(panelRules.every((rule) => rule.activation === "active")).toBe(true);
  });
});
