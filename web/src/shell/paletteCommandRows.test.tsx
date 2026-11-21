import { FlagIcon } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import {
  HIDDEN,
  type PaletteCommand,
  type PaletteCommandGroup,
  unavailableBecause,
} from "@/store/paletteCommands";
import { booleanSetting, settingCommand } from "@/store/paletteSettings";

import {
  currentPaletteCommand,
  paletteCommandItem,
  paletteCommandItems,
} from "./paletteCommandRows";

function command(id: string, overrides: Partial<PaletteCommand> = {}): PaletteCommand {
  return { id, label: id, icon: FlagIcon, keywords: [], run: vi.fn(), ...overrides };
}

const groups = (commands: PaletteCommand[]): Record<string, PaletteCommandGroup> => ({
  tabs: { heading: "Workspace", commands },
});

describe("paletteCommandItems", () => {
  it("lists the app's commands, then each feature's under its heading", () => {
    const items = paletteCommandItems([command("new-chat")], groups([command("back")]), "t");
    expect(items.map((item) => [item.id, item.group])).toEqual([
      ["command:new-chat", undefined],
      ["tabs:back", "Workspace"],
    ]);
  });

  it("passes on a command's opt-out from leading the sessions", () => {
    const items = paletteCommandItems(
      [command("open-pr", { leadsSources: false }), command("new-chat")],
      {},
      "t",
    );
    expect(items.map((item) => item.leadsSources)).toEqual([false, undefined]);
  });

  it("drops a hidden command and greys out one with a reason", () => {
    const items = paletteCommandItems(
      [
        command("hidden", { availability: HIDDEN }),
        command("greyed", { availability: unavailableBecause("Open a session first.") }),
      ],
      {},
      "t",
    );
    expect(items.map((item) => [item.id, item.disabledReason])).toEqual([
      ["command:greyed", "Open a session first."],
    ]);
  });

  it("shows a setting's current value and marks it, like a command with options, as a page", () => {
    const setting = settingCommand(
      booleanSetting({
        id: "always-steer",
        owner: "app",
        label: "Always steer",
        icon: FlagIcon,
        keywords: [],
        enabled: true,
        write: () => undefined,
      }),
    );
    const [settingRow, optionsRow] = paletteCommandItems(
      [setting, command("move", { options: () => [] })],
      {},
      "t",
    );
    expect(settingRow).toMatchObject({ detail: "On", opensPage: true });
    expect(optionsRow).toMatchObject({ opensPage: true });
  });

  it("shows a registered shortcut's key caps and a native one's chord", () => {
    const item = paletteCommandItem(command("settings", { shortcut: "settings.open" }), "t");
    // Open Settings is ⌘⌥, in a browser, where the desktop menu doesn't own it.
    expect(item.shortcut).toEqual([MOD_KEY, ALT_KEY, ","]);
    const native = paletteCommandItem(
      command("close", { nativeShortcut: { key: "w", mod: true } }),
      "t",
    );
    expect(native.shortcut?.at(-1)).toBe("W");
  });
});

describe("currentPaletteCommand", () => {
  const offered = (commands: PaletteCommand[], targetKey = "t") => ({
    appCommands: commands,
    featureGroups: {},
    targetKey,
  });

  it("finds the row's command as offered now", () => {
    const current = command("go");
    expect(currentPaletteCommand("command:go", "t", offered([current]))).toBe(current);
  });

  it("finds nothing for a row built for another target, withdrawn, or unavailable since", () => {
    expect(currentPaletteCommand("command:go", "old", offered([command("go")]))).toBeNull();
    expect(currentPaletteCommand("command:go", "t", offered([]))).toBeNull();
    const greyed = command("go", { availability: unavailableBecause("Not now.") });
    expect(currentPaletteCommand("command:go", "t", offered([greyed]))).toBeNull();
  });
});
