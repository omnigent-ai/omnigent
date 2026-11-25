import { describe, expect, it } from "vitest";

import { AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

import { goBack, historyStartingAt, recordStep } from "../workspaceTabs/workspaceHistory";
import type { Visit } from "../workspaceTabs/workspacePlace";
import {
  FIRST_TAB_REASON,
  NO_PULL_REQUESTS_REASON,
  showAllPullRequestsCommand,
  LAST_TAB_REASON,
  PINNED_EDGE_REASON,
  tabActionCommandFor,
  type WorkspaceTabAction,
  NO_CLOSED_TAB_REASON,
  NO_OTHER_TABS_REASON,
  SIDEBAR_CLOSED_REASON,
  NO_TAB_REASON,
  TAB_NOT_CLOSABLE_REASON,
  workspaceTabActionCommands,
  HISTORY_WAITING_REASON,
  INEXACT_PAGE_NOTE,
  browserTabInfo,
  NOTHING_BACK_REASON,
  NOTHING_FORWARD_REASON,
  PANEL_CLOSING_REASON,
  SHELL_EDIT_ACCESS_REASON,
  SHELL_OFFLINE_REASON,
  SHELL_STARTING_REASON,
  type ShellFacts,
  shellCommands,
  shellConnectState,
  type WorkspaceCommandFacts,
  closeTabLabel,
  workspaceCloseCommands,
  workspaceFullScreenCommand,
  workspaceOpenCommands,
  workspaceHistoryCommands,
  workspaceHistoryFacts,
  workspaceShowCommands,
  NO_TABS_REASON,
  SEARCH_TABS_LABEL,
} from "./model";
import { LAST_NEW_TAB_REASON } from "../workspaceTabs/workspaceTabsStore";

const SHELLS: ShellFacts = {
  declared: ["zsh", "bash", "fish"],
  defaultShell: "bash",
  connect: "ready",
  launching: false,
  canEdit: true,
};

function facts(overrides: Partial<WorkspaceCommandFacts> = {}): WorkspaceCommandFacts {
  return {
    openablePanels: [{ tab: "changes", label: "Changes" }],
    browser: true,
    sideChat: true,
    shells: SHELLS,
    openPanels: [],
    boundChords: { shell: true, browser: true },
    fullScreen: false,
    ...overrides,
  };
}

describe("shellCommands", () => {
  it("lists the default shell first, then the others in declaration order", () => {
    const commands = shellCommands(SHELLS, true);
    expect(commands.map((command) => [command.id, command.label, command.choiceLabel])).toEqual([
      ["shell", "Open a new shell (bash)", "bash (default)"],
      ["shell:zsh", "Open a new zsh shell", "zsh"],
      ["shell:fish", "Open a new fish shell", "fish"],
    ]);
    expect(commands.map((command) => command.isDefault)).toEqual([true, false, false]);
    expect(commands[0].menuLabel).toBe("Shell (bash)");
  });

  it("puts the new-shell chord on the default shell only where the chord acts", () => {
    expect(shellCommands(SHELLS, true).map((command) => command.shortcut)).toEqual([
      "terminal.new",
      undefined,
      undefined,
    ]);
    expect(shellCommands(SHELLS, false)[0].shortcut).toBeUndefined();
  });

  it("greys every shell for a viewer without edit access, before any other reason", () => {
    for (const command of shellCommands({ ...SHELLS, canEdit: false, connect: "offline" }, true)) {
      expect(command.availability).toEqual(unavailableBecause(SHELL_EDIT_ACCESS_REASON));
    }
  });

  it("greys every shell with a reason while offline or while one is starting", () => {
    for (const command of shellCommands({ ...SHELLS, connect: "offline" }, true)) {
      expect(command.availability).toEqual(unavailableBecause(SHELL_OFFLINE_REASON));
    }
    for (const command of shellCommands({ ...SHELLS, launching: true }, true)) {
      expect(command.availability).toEqual(unavailableBecause(SHELL_STARTING_REASON));
    }
  });

  it("says the default is reconnecting while it starts on a waking session", () => {
    const [waking] = shellCommands({ ...SHELLS, connect: "wakeable", launching: true }, true);
    expect(waking).toMatchObject({ reconnecting: true, menuLabel: "Reconnecting…" });
    const [ready] = shellCommands({ ...SHELLS, launching: true }, true);
    expect(ready).toMatchObject({ reconnecting: false, menuLabel: "Shell (bash)" });
  });
});

describe("shellConnectState", () => {
  it.each([
    [undefined, "ready"],
    [{ kind: "online" }, "ready"],
    [{ kind: "runner_asleep" }, "wakeable"],
    [{ kind: "host_asleep" }, "wakeable"],
    [{ kind: "starting" }, "wakeable"],
    [{ kind: "host_offline" }, "offline"],
    [{ kind: "local_stranded" }, "offline"],
  ] as const)("reads %j as %s", (liveness, state) => {
    expect(shellConnectState(liveness as Parameters<typeof shellConnectState>[0])).toBe(state);
  });
});

describe("workspaceOpenCommands", () => {
  it("marks the new-shell and new-browser chords separately, as each acts here", () => {
    const shortcuts = (boundChords: WorkspaceCommandFacts["boundChords"]) =>
      Object.fromEntries(
        workspaceOpenCommands(facts({ boundChords })).map((command) => [
          command.id,
          command.shortcut,
        ]),
      );
    // A grid cell's own browser chord, with the new-shell chord left to the routed session.
    expect(shortcuts({ shell: false, browser: true })).toMatchObject({
      browser: "browser.new",
      shell: undefined,
    });
    expect(shortcuts({ shell: true, browser: false })).toMatchObject({
      browser: undefined,
      shell: "terminal.new",
    });
  });

  it("lists panels, browser, side chat, and shells, in the + menu's order", () => {
    expect(workspaceOpenCommands(facts()).map((command) => command.id)).toEqual([
      "panel:changes",
      "browser",
      "side-chat",
      "shell",
      "shell:zsh",
      "shell:fish",
    ]);
  });

  it("names each action on its own in ⌘K and under its heading in the menu", () => {
    const [panel, browser, sideChat] = workspaceOpenCommands(facts());
    expect([panel.label, panel.menuLabel]).toEqual(["Open the Changes panel", "Changes"]);
    expect([browser.label, browser.menuLabel]).toEqual(["Open a new browser tab", "Browser"]);
    expect([sideChat.label, sideChat.menuLabel]).toEqual(["Open a new side chat", "Side chat"]);
    expect(browser.shortcut).toBe("browser.new");
  });

  it("leaves out what the session can't open", () => {
    const commands = workspaceOpenCommands(
      facts({ openablePanels: [], browser: false, sideChat: false, shells: null }),
    );
    expect(commands).toEqual([]);
  });
});

describe("workspaceShowCommands", () => {
  it("offers each open panel, marking the one on screen and greying one that's closing", () => {
    const commands = workspaceShowCommands(
      facts({
        openPanels: [
          { key: "files", label: "Files", active: true, closing: false, closable: false },
          { key: "terminal:zsh-1", label: "zsh 1", active: false, closing: true, closable: true },
        ],
      }),
    );
    expect(
      commands.map((command) => [command.id, command.label, command.active, command.availability]),
    ).toEqual([
      ["show:files", "Show Files", true, AVAILABLE],
      ["show:terminal:zsh-1", "Show zsh 1", false, unavailableBecause(PANEL_CLOSING_REASON)],
    ]);
  });
});

describe("workspaceCloseCommands", () => {
  it("offers a close for each tab with a close button, greying one that's closing", () => {
    const commands = workspaceCloseCommands(
      facts({
        openPanels: [
          { key: "files", label: "Files", active: true, closing: false, closable: false },
          { key: "changes", label: "Changes", active: false, closing: false, closable: true },
          {
            key: "file:src/a.ts",
            label: "src/a.ts",
            active: false,
            closing: false,
            closable: true,
          },
          { key: "terminal:zsh-1", label: "zsh 1", active: false, closing: true, closable: true },
        ],
      }),
    );
    expect(commands.map((command) => [command.id, command.label, command.availability])).toEqual([
      ["close:changes", "Close Changes", AVAILABLE],
      ["close:file:src/a.ts", "Close src/a.ts", AVAILABLE],
      ["close:terminal:zsh-1", "Close zsh 1", unavailableBecause(PANEL_CLOSING_REASON)],
    ]);
  });

  it("shares its label with the tab's close button", () => {
    expect(closeTabLabel("Browser 2")).toBe("Close Browser 2");
  });

  it("uses a tab's close name over its label", () => {
    const [command] = workspaceCloseCommands(
      facts({
        openPanels: [
          {
            key: "file:src/a.ts",
            label: "src/a.ts",
            active: false,
            closing: false,
            closable: true,
            closeName: "a.ts",
          },
        ],
      }),
    );
    expect(command.label).toBe("Close a.ts");
  });

  it("offers nothing when no tab can be closed", () => {
    expect(workspaceCloseCommands(facts())).toEqual([]);
  });
});

describe("workspaceFullScreenCommand", () => {
  it("names what the toggle does now", () => {
    expect(workspaceFullScreenCommand(facts({ fullScreen: false }))).toMatchObject({
      id: "full-screen",
      label: "Make the workspace full screen",
      menuLabel: "Full screen",
      availability: AVAILABLE,
    });
    expect(workspaceFullScreenCommand(facts({ fullScreen: true }))).toMatchObject({
      label: "Exit workspace full screen",
      menuLabel: "Exit full screen",
    });
  });

  it("is absent where the workspace can't go full screen", () => {
    expect(workspaceFullScreenCommand(facts({ fullScreen: null }))).toBeNull();
  });
});

describe("workspace history commands", () => {
  const visit = (tabId: string, path: string): Visit => ({
    tabId,
    place: { kind: "files", path },
  });
  const history = goBack(
    recordStep(
      recordStep(historyStartingAt(visit("F", "a.ts")), visit("F", "b.ts")),
      visit("W", "c.ts"),
    ),
  );

  it("names where Back and Forward land", () => {
    const historyFacts = workspaceHistoryFacts(history, (target) =>
      String(target.place.kind === "files" && target.place.path),
    );
    expect(historyFacts).toEqual({
      back: { target: "a.ts", waitingForPage: false, inexactPage: false },
      forward: { target: "c.ts", waitingForPage: false, inexactPage: false },
    });
    expect(
      workspaceHistoryCommands(historyFacts).map((command) => [
        command.menuLabel,
        command.availability,
      ]),
    ).toEqual([
      ["Back to a.ts", { available: true }],
      ["Forward to c.ts", { available: true }],
    ]);
  });

  it("disables, without skipping, a step onto a page that is still loading", () => {
    const historyFacts = workspaceHistoryFacts(
      history,
      () => "x",
      (target) => target.tabId === "W",
    );
    expect(historyFacts.forward).toEqual({
      target: null,
      waitingForPage: true,
      inexactPage: false,
    });
    const forward = workspaceHistoryCommands(historyFacts)[1]!;
    expect(forward.menuLabel).toBe("Forward");
    expect(forward.availability).toEqual(unavailableBecause(HISTORY_WAITING_REASON));
  });

  it("notes a step onto a page this shell can only reload by URL", () => {
    const historyFacts = workspaceHistoryFacts(
      history,
      () => "x",
      () => false,
      (target) => target.tabId === "W",
    );
    const [back, forward] = workspaceHistoryCommands(historyFacts);
    expect(back!.note).toBeUndefined();
    expect(forward!.note).toBe(INEXACT_PAGE_NOTE);
    expect(forward!.availability).toEqual({ available: true });
  });

  it("states an older app's page history limits in a browser tab's info", () => {
    expect(browserTabInfo("x.test/a", true)).toBe("x.test/a");
    const info = browserTabInfo("x.test/a", false);
    expect(info.startsWith(`x.test/a\n\n${INEXACT_PAGE_NOTE}`)).toBe(true);
    expect(info).toContain("counts as where that step landed");
    expect(info).toContain("fails to load");
  });

  it("says when there is nowhere to go", () => {
    const historyFacts = workspaceHistoryFacts(historyStartingAt(visit("F", "a.ts")), () => "x");
    expect(workspaceHistoryCommands(historyFacts).map((command) => command.availability)).toEqual([
      unavailableBecause(NOTHING_BACK_REASON),
      unavailableBecause(NOTHING_FORWARD_REASON),
    ]);
  });
});

describe("workspaceTabActionCommands", () => {
  it("keeps New tab and Reopen available with the sidebar closed and says why the others wait", () => {
    const commands = workspaceTabActionCommands({
      canReopen: true,
      selectedIndex: 1,
      tabCount: 3,
      sidebarOpen: false,
    });
    const byAction = Object.fromEntries(
      commands.map((command) => [
        command.kind === "tab-action" ? command.action : command.id,
        command.availability,
      ]),
    );
    expect(byAction["new-tab"]).toEqual(AVAILABLE);
    expect(byAction.reopen).toEqual(AVAILABLE);
    for (const action of [
      "close-tab",
      "close-others",
      "move-left",
      "move-right",
      "toggle-pin",
      "search-tabs",
    ]) {
      expect(byAction[action]).toEqual(unavailableBecause(SIDEBAR_CLOSED_REASON));
    }
  });

  it("explains each unavailable action while the sidebar is open", () => {
    const commands = workspaceTabActionCommands({
      canReopen: false,
      selectedIndex: 0,
      tabCount: 1,
    });
    expect(commands.map((command) => command.availability)).toEqual([
      AVAILABLE,
      AVAILABLE,
      unavailableBecause(NO_CLOSED_TAB_REASON),
      unavailableBecause(NO_OTHER_TABS_REASON),
      unavailableBecause(FIRST_TAB_REASON),
      unavailableBecause(LAST_TAB_REASON),
      AVAILABLE,
      AVAILABLE,
    ]);
  });

  describe("with pinned tabs", () => {
    const availabilityOf = (
      action: WorkspaceTabAction,
      overrides: Partial<Parameters<typeof workspaceTabActionCommands>[0]>,
    ) =>
      tabActionCommandFor(
        workspaceTabActionCommands({
          canReopen: false,
          selectedIndex: 0,
          tabCount: 4,
          ...overrides,
        }),
        action,
      ).availability;

    it("stops moves at the pinned tabs' edge from either side, and says why", () => {
      // Two pinned tabs, then two others.
      const pinned = { pinnedCount: 2, selectedPinned: true };
      expect(availabilityOf("move-left", { ...pinned, selectedIndex: 0 })).toEqual(
        unavailableBecause(FIRST_TAB_REASON),
      );
      expect(availabilityOf("move-right", { ...pinned, selectedIndex: 0 })).toEqual(AVAILABLE);
      expect(availabilityOf("move-right", { ...pinned, selectedIndex: 1 })).toEqual(
        unavailableBecause(PINNED_EDGE_REASON),
      );
      const other = { pinnedCount: 2, selectedPinned: false };
      expect(availabilityOf("move-left", { ...other, selectedIndex: 2 })).toEqual(
        unavailableBecause(PINNED_EDGE_REASON),
      );
      expect(availabilityOf("move-left", { ...other, selectedIndex: 3 })).toEqual(AVAILABLE);
      expect(availabilityOf("move-right", { ...other, selectedIndex: 3 })).toEqual(
        unavailableBecause(LAST_TAB_REASON),
      );
      // With every tab pinned, the last one is simply last.
      expect(
        availabilityOf("move-right", { pinnedCount: 4, selectedPinned: true, selectedIndex: 3 }),
      ).toEqual(unavailableBecause(LAST_TAB_REASON));
    });

    it("pins or unpins the selected tab, and says why a split can't be pinned", () => {
      const pin = (overrides: Partial<Parameters<typeof workspaceTabActionCommands>[0]>) =>
        tabActionCommandFor(
          workspaceTabActionCommands({
            canReopen: false,
            selectedIndex: 0,
            tabCount: 2,
            ...overrides,
          }),
          "toggle-pin",
        );
      expect(pin({})).toMatchObject({
        label: "Pin workspace tab",
        menuLabel: "Pin tab",
        availability: AVAILABLE,
      });
      expect(pin({ selectedPinned: true })).toMatchObject({
        label: "Unpin workspace tab",
        menuLabel: "Unpin tab",
      });
      expect(pin({ selectedPinRefusal: "This tab can't be pinned." }).availability).toEqual(
        unavailableBecause("This tab can't be pinned."),
      );
      expect(pin({ selectedIndex: -1 }).availability).toEqual(unavailableBecause(NO_TAB_REASON));
    });
  });

  it("offers one Search tabs… command, with no chord, wherever tabs are open", () => {
    const search = (overrides: Partial<Parameters<typeof workspaceTabActionCommands>[0]>) =>
      workspaceTabActionCommands({
        canReopen: false,
        selectedIndex: 0,
        tabCount: 2,
        ...overrides,
      }).find((command) => command.action === "search-tabs")!;
    expect(search({})).toMatchObject({
      id: "tab-action:search-tabs",
      label: SEARCH_TABS_LABEL,
      menuLabel: "All tabs",
      availability: AVAILABLE,
    });
    expect(search({}).shortcut).toBeUndefined();
    expect(search({ tabCount: 0, selectedIndex: -1 }).availability).toEqual(
      unavailableBecause(NO_TABS_REASON),
    );
    expect(search({ sidebarOpen: false }).availability).toEqual(
      unavailableBecause(SIDEBAR_CLOSED_REASON),
    );
  });

  it("offers a group's close only where the tab list groups tabs, counting what it closes", () => {
    const base = { canReopen: false, selectedIndex: 0, tabCount: 4 };
    expect(
      workspaceTabActionCommands(base).some((command) =>
        [
          "close-merged-pull-requests",
          "close-pull-request-tabs",
          "close-file-tabs",
          "close-page-tabs",
        ].includes(command.action),
      ),
    ).toBe(false);
    const commands = workspaceTabActionCommands({
      ...base,
      groupCloseCounts: {
        "close-merged-pull-requests": 1,
        "close-pull-request-tabs": 3,
        "close-file-tabs": 0,
        "close-page-tabs": 2,
      },
    });
    const merged = tabActionCommandFor(commands, "close-merged-pull-requests");
    expect(merged).toMatchObject({
      label: "Close merged pull request tabs",
      menuLabel: "Close merged (1)",
      availability: AVAILABLE,
    });
    expect(tabActionCommandFor(commands, "close-pull-request-tabs").label).toBe(
      "Close all pull request tabs",
    );
    expect(tabActionCommandFor(commands, "close-page-tabs").menuLabel).toBe("Close all");
    expect(tabActionCommandFor(commands, "close-file-tabs").availability).toEqual(
      unavailableBecause("No file tabs can close."),
    );
    const closed = workspaceTabActionCommands({
      ...base,
      sidebarOpen: false,
      groupCloseCounts: {
        "close-merged-pull-requests": 1,
        "close-pull-request-tabs": 1,
        "close-file-tabs": 1,
        "close-page-tabs": 1,
      },
    });
    expect(tabActionCommandFor(closed, "close-merged-pull-requests").availability).toEqual(
      unavailableBecause(SIDEBAR_CLOSED_REASON),
    );
  });
});

describe("the chorded tab actions", () => {
  it("bind New, Close and Reopen to their shortcuts, and Close names the tab", () => {
    const commands = workspaceTabActionCommands({
      canReopen: true,
      selectedIndex: 0,
      tabCount: 1,
      selectedLabel: "main.go",
    });
    expect(
      commands
        .filter((command) => command.shortcut)
        .map((command) => [command.action, command.shortcut, command.menuLabel]),
    ).toEqual([
      ["new-tab", "workspace.newTab", "New tab"],
      ["close-tab", "workspace.closeTab", "Close main.go"],
      ["reopen", "workspace.reopenTab", "Reopen closed tab"],
    ]);
  });

  it("says why Close waits: nothing selected, or a shell without edit access", () => {
    const close = (overrides: Partial<Parameters<typeof workspaceTabActionCommands>[0]>) =>
      workspaceTabActionCommands({
        canReopen: false,
        selectedIndex: 0,
        tabCount: 1,
        ...overrides,
      }).find((command) => command.action === "close-tab")!.availability;
    expect(close({ selectedIndex: -1 })).toEqual(unavailableBecause(NO_TAB_REASON));
    expect(close({ selectedCloseRefusal: TAB_NOT_CLOSABLE_REASON })).toEqual(
      unavailableBecause(TAB_NOT_CLOSABLE_REASON),
    );
    // The only empty New tab stays, and Close says so rather than doing nothing.
    expect(close({ selectedCloseRefusal: LAST_NEW_TAB_REASON })).toEqual(
      unavailableBecause(LAST_NEW_TAB_REASON),
    );
  });
});

describe("showAllPullRequestsCommand", () => {
  it("is available where the workspace shows pull requests, or a pull request tab is open", () => {
    const available = (pullRequestsAvailable: boolean, hasPullRequestTab: boolean) =>
      showAllPullRequestsCommand({ pullRequestsAvailable, hasPullRequestTab }).availability;
    expect(available(true, false)).toEqual(AVAILABLE);
    expect(available(false, true)).toEqual(AVAILABLE);
    expect(available(false, false)).toEqual(unavailableBecause(NO_PULL_REQUESTS_REASON));
    expect(
      showAllPullRequestsCommand({ pullRequestsAvailable: true, hasPullRequestTab: false }).label,
    ).toBe("Show all pull requests");
  });
});
