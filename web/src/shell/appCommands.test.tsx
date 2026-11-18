import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

import { type ActionCommand, filterAppCommands, useAppCommands } from "./appCommands";

function commands(rightSidebarShortcutEnabled = true): ActionCommand[] {
  return renderHook(() =>
    useAppCommands({
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
      rightSidebarShortcutEnabled,
    }),
  ).result.current;
}

describe("useAppCommands", () => {
  it("lists the app's commands in palette order", () => {
    expect(commands().map((command) => command.label)).toEqual([
      "New chat",
      "Go to Inbox",
      "Go to Automations",
      "Go to Settings",
      "Toggle conversations sidebar",
      "Toggle workspace sidebar",
    ]);
  });

  it("navigates for the Go to commands", () => {
    const byId = new Map(commands().map((command) => [command.id, command]));
    byId.get("go-inbox")!.run();
    byId.get("go-settings")!.run();
    expect(navigate.mock.calls).toEqual([["/inbox"], ["/settings"]]);
  });

  it("drops the workspace sidebar's chord where the route owns it", () => {
    const toggle = (enabled: boolean) =>
      commands(enabled).find((command) => command.id === "toggle-right-sidebar")!;
    expect(toggle(true).shortcut).toBeDefined();
    expect(toggle(false).shortcut).toBeUndefined();
  });
});

describe("filterAppCommands", () => {
  const all = commands();

  it("returns every command for a blank query", () => {
    expect(filterAppCommands(all, "  ")).toBe(all);
  });

  it("matches the label or a keyword, ignoring case and outer spaces", () => {
    expect(filterAppCommands(all, " SETT ").map((command) => command.id)).toEqual(["go-settings"]);
    expect(filterAppCommands(all, "cron").map((command) => command.id)).toEqual(["go-tasks"]);
    expect(filterAppCommands(all, "zzz")).toEqual([]);
  });
});
