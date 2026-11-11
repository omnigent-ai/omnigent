import { describe, expect, it } from "vitest";

import {
  resolveWorkspaceActiveView,
  type WorkspaceSelection,
  type WorkspaceViewAvailability,
} from "./workspaceActiveView";

const NOTHING_SELECTED: WorkspaceSelection = {
  rightRailTab: "files",
  selectedFilePath: null,
  selectedTerminalKey: null,
  selectedBrowserId: null,
  selectedSideChatId: null,
};

const EVERYTHING_AVAILABLE: WorkspaceViewAvailability = {
  terminalPresent: () => true,
  showBrowserTab: true,
  showGithubTab: true,
  showContentSearch: true,
  hasAgentTree: true,
  mobile: false,
};

function resolve(
  selection: Partial<WorkspaceSelection>,
  availability: Partial<WorkspaceViewAvailability> = {},
) {
  return resolveWorkspaceActiveView(
    { ...NOTHING_SELECTED, ...selection },
    { ...EVERYTHING_AVAILABLE, ...availability },
  );
}

describe("resolveWorkspaceActiveView", () => {
  it.each([
    ["the Files tab", { rightRailTab: "files" }, {}, "files"],
    ["the Changes tab", { rightRailTab: "changes" }, {}, "files"],
    ["Pull Requests", { rightRailTab: "github" }, {}, "github"],
    [
      "Pull Requests where they're hidden",
      { rightRailTab: "github" },
      { showGithubTab: false },
      "files",
    ],
    ["Agents", { rightRailTab: "subagents" }, {}, "subagents"],
    [
      "Agents without an agent tree",
      { rightRailTab: "subagents" },
      { hasAgentTree: false },
      "files",
    ],
    ["content search", { rightRailTab: "content-search" }, {}, "content-search"],
    [
      "content search where it's off",
      { rightRailTab: "content-search" },
      { showContentSearch: false },
      "files",
    ],
    ["no tab", { rightRailTab: null }, {}, "empty"],
    ["a browser tab", { rightRailTab: "browser", selectedBrowserId: "b1" }, {}, "browser"],
    [
      "a browser tab where browsers are hidden",
      { rightRailTab: "browser", selectedBrowserId: "b1" },
      { showBrowserTab: false },
      "files",
    ],
    ["the browser mode with no browser selected", { rightRailTab: "browser" }, {}, "files"],
    ["a side chat", { rightRailTab: "sidechat", selectedSideChatId: "c1" }, {}, "sidechat"],
    [
      "a side chat on a phone, which shows it in a drawer",
      { rightRailTab: "sidechat", selectedSideChatId: "c1" },
      { mobile: true },
      "files",
    ],
    ["the side-chat mode with no side chat selected", { rightRailTab: "sidechat" }, {}, "files"],
  ] as const)("shows %s as the %s page", (_name, selection, availability, scope) => {
    expect(resolve(selection, availability).scope).toBe(scope);
  });

  it("lists only changes on the Changes tab", () => {
    expect(resolve({ rightRailTab: "changes" }).flat).toBe(true);
    expect(resolve({ rightRailTab: "files" }).flat).toBe(false);
  });

  it("puts a present shell in front of an open file", () => {
    const view = resolve({ selectedTerminalKey: "terminal:zsh", selectedFilePath: "a.ts" });
    expect(view).toMatchObject({ terminalKey: "terminal:zsh", filePath: null });
  });

  it("shows the open file while the selected shell's terminal is missing", () => {
    const view = resolve(
      { selectedTerminalKey: "terminal:zsh", selectedFilePath: "a.ts" },
      { terminalPresent: (key) => key !== "terminal:zsh" },
    );
    expect(view).toMatchObject({ terminalKey: null, filePath: "a.ts" });
  });

  it.each([
    ["an open file", { selectedFilePath: "a.ts" }],
    ["a selected shell, even with its terminal missing", { selectedTerminalKey: "terminal:zsh" }],
  ])("doesn't select a browser or side chat behind %s", (_name, inFront) => {
    const availability = { terminalPresent: () => false };
    const browser = resolve(
      { rightRailTab: "browser", selectedBrowserId: "b1", ...inFront },
      availability,
    );
    const sideChat = resolve(
      { rightRailTab: "sidechat", selectedSideChatId: "c1", ...inFront },
      availability,
    );
    expect(browser).toMatchObject({ browserSelected: false, scope: "files" });
    expect(sideChat).toMatchObject({ sideChatSelected: false, scope: "files" });
  });

  it("keeps a side chat selected on a phone though the rail doesn't show it", () => {
    const view = resolve({ rightRailTab: "sidechat", selectedSideChatId: "c1" }, { mobile: true });
    expect(view.sideChatSelected).toBe(true);
  });
});
