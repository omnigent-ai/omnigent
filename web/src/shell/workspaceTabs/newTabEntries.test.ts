import { describe, expect, it } from "vitest";

import {
  workspaceOpenCommands,
  type ShellFacts,
  type WorkspaceCommandFacts,
} from "@/shell/workspaceCommands/model";
import { HIDDEN, unavailableBecause } from "@/store/paletteCommands";

import { newTabEntries, SHELL_ROW_LIMIT, shellChoiceEntries } from "./newTabEntries";

const shells: ShellFacts = {
  declared: ["zsh", "bash"],
  defaultShell: "zsh",
  connect: "ready",
  launching: false,
  canEdit: true,
};

function facts(overrides: Partial<WorkspaceCommandFacts> = {}): WorkspaceCommandFacts {
  return {
    openablePanels: [
      { tab: "subagents", label: "Agents" },
      { tab: "github", label: "Pull Requests" },
      { tab: "files", label: "Files" },
      { tab: "changes", label: "Diffs" },
    ],
    browser: true,
    sideChat: true,
    shells,
    openPanels: [],
    boundChords: { shell: true, browser: true },
    fullScreen: null,
    ...overrides,
  };
}

function entries(overrides: Partial<WorkspaceCommandFacts> = {}, textSearch = true) {
  return newTabEntries({ commands: workspaceOpenCommands(facts(overrides)), textSearch });
}

describe("newTabEntries", () => {
  it("lists every app in a fixed order whatever order the commands come in", () => {
    expect(entries().map((entry) => entry.id)).toEqual([
      "terminal",
      "side-chat",
      "changes",
      "files",
      "shell:bash",
      "pulls",
      "agents",
      "browser",
      "search",
    ]);
  });

  it("maps apps to the places they show and the shell to its command", () => {
    const byId = Object.fromEntries(entries().map((entry) => [entry.id, entry]));
    expect(byId.files.action).toEqual({ kind: "place", place: { kind: "files" } });
    expect(byId.changes.action).toEqual({ kind: "place", place: { kind: "changes" } });
    expect(byId.pulls.action).toEqual({ kind: "place", place: { kind: "pulls" } });
    expect(byId.agents.action).toEqual({ kind: "place", place: { kind: "agents" } });
    expect(byId["side-chat"].action).toEqual({ kind: "place", place: { kind: "chat" } });
    expect(byId.browser.action).toEqual({ kind: "place", place: { kind: "web" } });
    expect(byId.search).toMatchObject({
      label: "Search text",
      shortcut: "palette.text",
      action: { kind: "text-search" },
    });
    // The card shows ⌃`, as ⌘K and the "+" menu do.
    expect(byId.terminal).toMatchObject({
      label: "Terminal",
      description: "zsh",
      shortcut: "terminal.new",
      action: { kind: "command", command: { kind: "shell", shell: "zsh", isDefault: true } },
    });
    expect(byId.browser.shortcut).toBe("browser.new");
    expect(byId.changes.label).toBe("Diffs");
    expect(byId["shell:bash"]).toMatchObject({
      label: "bash",
      description: "Terminal",
      action: { kind: "command", command: { kind: "shell", shell: "bash", isDefault: false } },
    });
    // ⌃` opens the default shell, the Terminal card's; another shell's row has no chord.
    expect(byId["shell:bash"].shortcut).toBeUndefined();
  });

  it("keeps unavailable apps listed with the reason", () => {
    const byId = Object.fromEntries(
      entries({
        openablePanels: [
          { tab: "files", label: "Files" },
          {
            tab: "github",
            label: "Pull Requests",
            availability: unavailableBecause("No pull requests are linked to this session."),
          },
        ],
        shells: { ...shells, connect: "offline" },
      }).map((entry) => [entry.id, entry]),
    );
    expect(byId.pulls.disabledReason).toBe("No pull requests are linked to this session.");
    expect(byId.terminal.disabledReason).toBe(
      "This session is offline. Reconnect it to open a shell.",
    );
    expect(byId["shell:bash"].disabledReason).toBe(byId.terminal.disabledReason);
    expect(byId.files.disabledReason).toBeUndefined();
  });

  it("lists Tasks after Agents, and maps it to the Tasks place, when the session has one", () => {
    const withTasks = entries({
      openablePanels: [...facts().openablePanels, { tab: "tasks", label: "Tasks" }],
    });
    expect(withTasks.map((entry) => entry.id)).toEqual([
      "terminal",
      "side-chat",
      "changes",
      "files",
      "shell:bash",
      "pulls",
      "agents",
      "tasks",
      "browser",
      "search",
    ]);
    expect(withTasks.find((entry) => entry.id === "tasks")?.action).toEqual({
      kind: "place",
      place: { kind: "tasks" },
    });
  });

  it("leaves out apps this workspace doesn't have", () => {
    const ids = entries({ shells: null, browser: false, sideChat: false }, false).map(
      (entry) => entry.id,
    );
    expect(ids).toEqual(["changes", "files", "pulls", "agents"]);
  });

  it("says when the default shell is reconnecting and drops chords that don't reach this workspace", () => {
    const [terminal] = entries({
      shells: { ...shells, connect: "wakeable", launching: true },
      boundChords: { shell: false, browser: false },
    });
    expect(terminal).toMatchObject({ label: "Terminal (reconnecting…)", shortcut: undefined });
    expect(terminal.disabledReason).toBe("A shell is starting…");
  });
});

describe("other shells", () => {
  const shellIds = (declared: string[]) =>
    entries({ shells: { ...shells, declared } })
      .map((entry) => entry.id)
      .filter((id) => id.startsWith("shell:") || id === "choose-shell" || id === "terminal");

  it("gives no other row to a single shell", () => {
    expect(shellIds(["zsh"])).toEqual(["terminal"]);
  });

  it("gives each other shell its own row, in declared order, while they fit", () => {
    expect(shellIds(["zsh", "bash"])).toEqual(["terminal", "shell:bash"]);
    expect(shellIds(["zsh", "bash", "fish", "nu"])).toHaveLength(1 + SHELL_ROW_LIMIT);
    expect(shellIds(["zsh", "bash", "fish", "nu"])).not.toContain("choose-shell");
  });

  it("keeps the first few rows and lists the rest behind Choose shell…", () => {
    const many = ["zsh", "bash", "fish", "nu", "sh"];
    expect(shellIds(many)).toEqual([
      "terminal",
      "shell:bash",
      "shell:fish",
      "shell:nu",
      "choose-shell",
    ]);
    const chooser = entries({ shells: { ...shells, declared: many } }).find(
      (entry) => entry.id === "choose-shell",
    );
    expect(chooser).toMatchObject({ label: "Choose shell…", description: "5 shells" });
  });

  it("leaves out a hidden shell's row", () => {
    const commands = workspaceOpenCommands(facts()).map((command) =>
      command.kind === "shell" && !command.isDefault
        ? { ...command, availability: HIDDEN }
        : command,
    );
    expect(newTabEntries({ commands, textSearch: false }).map((entry) => entry.id)).not.toContain(
      "shell:bash",
    );
  });
});

describe("hidden commands", () => {
  it("leaves out hidden panels, unoffered text search and shells, as ⌘K does", () => {
    const ids = newTabEntries({
      commands: workspaceOpenCommands(
        facts({
          openablePanels: [
            { tab: "files", label: "Files" },
            { tab: "changes", label: "Diffs", availability: HIDDEN },
          ],
        }),
      ),
      textSearch: false,
    }).map((entry) => entry.id);
    expect(ids).toEqual(["terminal", "side-chat", "files", "shell:bash", "browser"]);
    const hiddenShells = workspaceOpenCommands(facts()).map((command) =>
      command.kind === "shell" ? { ...command, availability: HIDDEN } : command,
    );
    expect(
      newTabEntries({ commands: hiddenShells, textSearch: false }).map((entry) => entry.id),
    ).not.toContain("terminal");
    expect(shellChoiceEntries(hiddenShells)).toEqual([]);
  });
});

describe("shellChoiceEntries", () => {
  it("lists every declared shell, the default first", () => {
    expect(
      shellChoiceEntries(workspaceOpenCommands(facts())).map((entry) => [entry.id, entry.label]),
    ).toEqual([
      ["shell:zsh", "zsh (default)"],
      ["shell:bash", "bash"],
    ]);
  });
});
