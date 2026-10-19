import { beforeEach, describe, expect, it } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "./sessionWorkspaceState";
import {
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
  writeDefaultWorkspaceTab,
  writePinnedWorkspaceTabs,
} from "./workspaceTabPreferences";

beforeEach(() => localStorage.clear());

it("round-trips workspace pins and default without restoring stale selections", () => {
  writePinnedWorkspaceTabs(["changes", "subagents"]);
  writeDefaultWorkspaceTab("changes");
  const exported = collectSettings()!;
  applyImportedSettings({ version: 1, settings: {} });
  expect(readPinnedWorkspaceTabs()).toHaveLength(4);
  expect(readInitialWorkspaceTab()).toBe("files");

  writeSessionWorkspaceState("conv", { open: true, rightRailTab: "files" });
  applyImportedSettings(exported);
  expect(readPinnedWorkspaceTabs()).toEqual(["changes", "subagents"]);
  expect(readInitialWorkspaceTab()).toBe("changes");
  expect(readSessionWorkspaceState("conv")).toEqual({ open: true });
});

describe("composer shortcut portability", () => {
  it("exports, imports, and clears the device-local preference", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    expect(collectSettings()?.settings[COMPOSER_SEND_SHORTCUT_STORAGE_KEY]).toBe("true");

    applyImportedSettings({ version: 1, settings: {} });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBeNull();

    applyImportedSettings({
      version: 1,
      settings: { [COMPOSER_SEND_SHORTCUT_STORAGE_KEY]: "true" },
    });
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBe("true");
  });
});
