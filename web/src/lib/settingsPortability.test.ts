import { beforeEach, describe, expect, it } from "vitest";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "./composerSendShortcutPreferences";
import { applyImportedSettings, collectSettings } from "./settingsPortability";
import {
  type SessionWorkspaceState,
  readSessionWorkspaceState,
  writeSessionWorkspaceState,
} from "./sessionWorkspaceState";
import {
  PINNED_WORKSPACE_TABS_STORAGE_KEY,
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

it("preserves unrelated session selections when importing changed pins without a new default", () => {
  writePinnedWorkspaceTabs(["files", "changes", "subagents"]);
  writeSessionWorkspaceState("files", {
    rightRailTab: "files",
    openFiles: ["README.md"],
    selectedFilePath: "README.md",
  });
  const openedPage: SessionWorkspaceState = { rightRailTab: "github", openPanels: ["github"] };
  writeSessionWorkspaceState("opened", openedPage);
  const browser: SessionWorkspaceState = {
    rightRailTab: "browser",
    selectedBrowserId: "one",
    openBrowsers: ["one"],
  };
  writeSessionWorkspaceState("browser", browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","subagents"]',
      "omnigent:default-workspace-tab": "files",
    },
  });
  expect(readSessionWorkspaceState("files")).toEqual({
    openFiles: ["README.md"],
    selectedFilePath: "README.md",
  });
  expect(readSessionWorkspaceState("opened")).toEqual(openedPage);
  expect(readSessionWorkspaceState("browser")).toEqual(browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","github","subagents"]',
      "omnigent:default-workspace-tab": "files",
    },
  });
  expect(readSessionWorkspaceState("opened")).toEqual(openedPage);
  expect(readSessionWorkspaceState("browser")).toEqual(browser);

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: '["changes","github","subagents"]',
      "omnigent:default-workspace-tab": "subagents",
    },
  });
  expect(readSessionWorkspaceState("opened")).toEqual({ openPanels: ["github"] });
  expect(readSessionWorkspaceState("browser")).toEqual({ openBrowsers: ["one"] });
});

it.each([
  ['["files","changes","subagents"]', "changes"],
  ['["changes","subagents"]', "subagents"],
] as const)("applies an explicit imported default with pins %s", (pins, defaultTab) => {
  writePinnedWorkspaceTabs(["files", "changes"]);
  writeSessionWorkspaceState("browser", {
    rightRailTab: "browser",
    openBrowsers: ["one"],
    selectedBrowserId: "one",
  });

  applyImportedSettings({
    version: 1,
    settings: {
      [PINNED_WORKSPACE_TABS_STORAGE_KEY]: pins,
      "omnigent:default-workspace-tab": defaultTab,
    },
  });

  expect(readInitialWorkspaceTab()).toBe(defaultTab);
  expect(readSessionWorkspaceState("browser")).toEqual({ openBrowsers: ["one"] });
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
