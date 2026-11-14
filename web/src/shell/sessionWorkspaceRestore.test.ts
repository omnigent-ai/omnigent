import { describe, expect, it } from "vitest";

import {
  CHAT_VIEW_STORAGE_VALUE,
  panelKeyStorageKey,
  resolveTerminalViewKey,
} from "./sessionWorkspaceRestore";

describe("panelKeyStorageKey", () => {
  it("keys each conversation's view choice by its id, unencoded", () => {
    expect(panelKeyStorageKey("conv_1")).toBe("omnigent.web.panel-key:conv_1");
    expect(panelKeyStorageKey("a/b c")).toBe("omnigent.web.panel-key:a/b c");
    expect(panelKeyStorageKey("conv_1")).not.toBe(panelKeyStorageKey("conv_2"));
  });
});

describe("resolveTerminalViewKey", () => {
  it("opens the stored terminal, so a shell opened from the rail isn't swapped for the agent's", () => {
    expect(resolveTerminalViewKey("terminal:shell_2", "terminal:agent")).toBe("terminal:shell_2");
  });

  it("keeps an empty stored target, which is still a stored choice", () => {
    expect(resolveTerminalViewKey("", "terminal:agent")).toBe("");
  });

  it("falls back to the agent terminal with nothing stored or Chat stored", () => {
    expect(resolveTerminalViewKey(null, "terminal:agent")).toBe("terminal:agent");
    expect(resolveTerminalViewKey(CHAT_VIEW_STORAGE_VALUE, "terminal:agent")).toBe(
      "terminal:agent",
    );
  });

  it("stores a Chat choice as the __chat__ marker", () => {
    expect(CHAT_VIEW_STORAGE_VALUE).toBe("__chat__");
  });
});
