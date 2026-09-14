import { describe, expect, it, vi } from "vitest";
import { ActionRegistry } from "./actionRegistry";
import { EMPTY_ACTION_CONTEXT } from "./context";
import { HANDLED, NOT_HANDLED, type ActionScopeId } from "./types";

const resolution = {
  context: EMPTY_ACTION_CONTEXT,
  focusedScopeIds: [] as ActionScopeId[],
};

describe("ActionRegistry", () => {
  it("keeps API-only actions out of keyboard dispatch until their migration", () => {
    const registry = new ActionRegistry();
    const run = vi.fn(() => HANDLED);
    registry.registerAction({ action: "session.action.new", scopeId: null, run });
    expect(registry.canHandle("session.action.new", resolution, { keyboardOnly: true })).toBe(
      false,
    );
    expect(registry.execute({ action: "session.action.new", source: "keyboard" }, resolution)).toBe(
      NOT_HANDLED,
    );
    expect(registry.execute({ action: "session.action.new", source: "palette" }, resolution)).toBe(
      HANDLED,
    );
  });

  it("executes the newest enabled handler and falls through notHandled", () => {
    const registry = new ActionRegistry();
    const older = vi.fn(() => HANDLED);
    const newer = vi.fn(() => NOT_HANDLED);
    registry.registerAction({ action: "session.action.new", scopeId: null, run: older });
    registry.registerAction({ action: "session.action.new", scopeId: null, run: newer });

    expect(registry.execute({ action: "session.action.new", source: "api" }, resolution)).toBe(
      HANDLED,
    );
    expect(newer).toHaveBeenCalledOnce();
    expect(older).toHaveBeenCalledOnce();
  });

  it("prefers the deepest focused scope, then active scopes, then global handlers", () => {
    const registry = new ActionRegistry();
    registry.registerScope({
      id: "file",
      parentId: null,
      mode: "fileViewer",
      active: true,
      context: { fileSearchOpen: true },
    });
    registry.registerScope({
      id: "editor",
      parentId: "file",
      mode: "markdownToc",
      active: true,
      context: { monacoFocus: true },
    });
    const calls: string[] = [];
    registry.registerAction({
      action: "file.action.find",
      scopeId: null,
      run: () => {
        calls.push("global");
      },
    });
    registry.registerAction({
      action: "file.action.find",
      scopeId: "file",
      run: () => {
        calls.push("file");
      },
    });
    registry.registerAction({
      action: "file.action.find",
      scopeId: "editor",
      run: (_invocation, { context }) => {
        expect(context.fileSearchOpen).toBe(true);
        expect(context.monacoFocus).toBe(true);
        calls.push("editor");
      },
    });

    const focused = {
      ...resolution,
      focusedScopeIds: ["editor", "file"] as ActionScopeId[],
    };
    expect(registry.execute({ action: "file.action.find", source: "api" }, focused)).toBe(HANDLED);
    expect(calls).toEqual(["editor"]);
  });

  it("routes save to the higher-priority editor handler in one file scope", () => {
    const registry = new ActionRegistry();
    registry.registerScope({
      id: "file",
      parentId: null,
      mode: "fileViewer",
      active: true,
      context: {},
    });
    const markdown = vi.fn(() => HANDLED);
    const monaco = vi.fn(() => HANDLED);
    registry.registerAction({
      action: "file.action.save",
      scopeId: "file",
      run: markdown,
    });
    const unregisterMonaco = registry.registerAction({
      action: "file.action.save",
      scopeId: "file",
      priority: 10,
      run: monaco,
    });
    const focused = { ...resolution, focusedScopeIds: ["file"] as ActionScopeId[] };
    registry.execute({ action: "file.action.save", source: "api" }, focused);
    expect(monaco).toHaveBeenCalledOnce();
    expect(markdown).not.toHaveBeenCalled();
    unregisterMonaco();
    registry.execute({ action: "file.action.save", source: "api" }, focused);
    expect(markdown).toHaveBeenCalledOnce();
  });

  it("skips inactive scoped handlers and disabled handlers", () => {
    const registry = new ActionRegistry();
    registry.registerScope({
      id: "closed",
      parentId: null,
      mode: "filesPanel",
      active: false,
      context: {},
    });
    registry.registerAction({
      action: "panel.action.closeFiles",
      scopeId: "closed",
      run: vi.fn(),
    });
    registry.registerAction({
      action: "panel.action.closeFiles",
      scopeId: null,
      isEnabled: () => false,
      run: vi.fn(),
    });
    expect(registry.execute({ action: "panel.action.closeFiles", source: "api" }, resolution)).toBe(
      NOT_HANDLED,
    );
  });

  it("observes rejected async handlers while treating started work as handled", async () => {
    const registry = new ActionRegistry();
    const error = new Error("boom");
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    registry.registerAction({
      action: "session.action.new",
      scopeId: null,
      run: () => Promise.reject(error),
    });
    expect(registry.execute({ action: "session.action.new", source: "api" }, resolution)).toBe(
      HANDLED,
    );
    await Promise.resolve();
    await Promise.resolve();
    expect(log).toHaveBeenCalledWith("Action session.action.new failed", error);
    log.mockRestore();
  });

  it("publishes visible action availability and registration cleanup", () => {
    const registry = new ActionRegistry();
    const listener = vi.fn();
    registry.subscribe(listener);
    const unregister = registry.registerAction({
      action: "session.action.new",
      scopeId: null,
      isEnabled: () => false,
      run: vi.fn(),
    });
    expect(registry.listAvailable(resolution)).toContainEqual(
      expect.objectContaining({ id: "session.action.new", enabled: false }),
    );
    unregister();
    expect(
      registry.listAvailable(resolution).some((item) => item.id === "session.action.new"),
    ).toBe(false);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("filters hidden and non-palette actions for consumers", () => {
    const registry = new ActionRegistry();
    registry.registerAction({
      action: "composer.action.send",
      scopeId: null,
      isVisible: () => false,
      run: vi.fn(),
    });
    registry.registerAction({
      action: "composer.action.recallNext",
      scopeId: null,
      run: vi.fn(),
    });
    expect(
      registry.listAvailable(resolution).some((item) => item.id === "composer.action.send"),
    ).toBe(false);
    expect(
      registry
        .listAvailable(resolution, { paletteOnly: true })
        .some((item) => item.id === "composer.action.recallNext"),
    ).toBe(false);
  });

  it("reports focused and active modes separately", () => {
    const registry = new ActionRegistry();
    registry.registerScope({
      id: "file",
      parentId: null,
      mode: "fileViewer",
      active: true,
      context: {},
    });
    registry.registerScope({
      id: "terminal",
      parentId: null,
      mode: "terminal",
      active: true,
      context: {},
    });
    expect(registry.getFocusedModes(["file" as ActionScopeId])).toEqual(
      new Set(["global", "fileViewer"]),
    );
    expect(registry.getActiveModes()).toEqual(new Set(["global", "fileViewer", "terminal"]));
  });
});
