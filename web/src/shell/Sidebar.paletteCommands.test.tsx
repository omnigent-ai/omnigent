// The sidebar's view, project list and selection actions in ⌘K: registered by the sidebar
// itself from the same models its headers and menus render, running the same handlers.

import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ConversationsModule from "@/hooks/useConversations";
import type * as ProjectsApiModule from "@/lib/projectsApi";
import { type PaletteCommand, usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  chooseSettingValue,
  expectSetting,
  settingIn,
  settingValues,
} from "@/test/paletteCommands";
import { conversation, conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    PINNED_CONVERSATIONS_KEY: actual.PINNED_CONVERSATIONS_KEY,
    useProjects: vi.fn(() => ({ data: [] })),
  };
});
const projectOrder = vi.hoisted(() => ({ settled: true }));
vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ProjectsApiModule>()),
  getProjectOrder: () =>
    projectOrder.settled
      ? Promise.resolve({ sort_mode: "manual", ordered_project_ids: ["project-a", "project-b"] })
      : new Promise(() => {}),
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations, useProjects } from "@/hooks/useConversations";

const LOOSE = conversation("loose1", { title: "Loose session" });
const QUICK_PICK = { quick_pick_palette: true };

beforeEach(() => {
  projectOrder.settled = true;
  vi.mocked(useConversations).mockImplementation(() => conversationPage([LOOSE]));
  vi.mocked(useProjects).mockReturnValue({
    data: [
      { id: "project-a", name: "Alpha" },
      { id: "project-b", name: "Beta" },
    ],
  } as unknown as ReturnType<typeof useProjects>);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  usePaletteCommandsStore.setState({ groups: {} });
});

const commands = (): readonly PaletteCommand[] =>
  usePaletteCommandsStore.getState().groups["sidebar-projects"]?.commands ?? [];
const labels = () => commands().map((command) => command.label);

const viewCommands = (): readonly PaletteCommand[] =>
  usePaletteCommandsStore.getState().groups["sidebar-view"]?.commands ?? [];
const chooseView = (setting: string, value: string) =>
  act(() => void chooseSettingValue(settingIn(viewCommands(), setting), value));

describe("the session list's view in ⌘K", () => {
  it("shows the display as a setting, and choosing a value updates the menu", async () => {
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() =>
      expect(settingValues(viewCommands())["Session List: Display"]).toBe("My sessions"),
    );

    chooseView("Session List: Display", "Archived sessions");
    await waitFor(() =>
      expect(settingValues(viewCommands())["Session List: Display"]).toBe("Archived sessions"),
    );
    fireEvent.pointerDown(screen.getByTestId("session-filter"), { button: 0 });
    expect(await screen.findByTestId("session-filter-archived")).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("offers grouping, ordering and fields with sidebar_grouping_ui, and regroups the list", async () => {
    renderSidebar({ features: { ...QUICK_PICK, sidebar_grouping_ui: true } });
    await waitFor(() =>
      expect(settingValues(viewCommands())["Session List: Group by"]).toBe("Default"),
    );
    expectSetting(viewCommands(), "Session List: Group by", {
      current: "Default",
      choices: ["Default", "Status", "Harness", "Created", "Updated"],
    });
    expectSetting(viewCommands(), "Session List: Display", { current: "My sessions" });
    expectSetting(viewCommands(), "Session List: Sort by", {
      choices: ["Updated", "Created", "Custom"],
    });
    const onOff = { choices: ["On", "Off"] };
    expectSetting(viewCommands(), "Session List: Show Last Updated Time", onOff);
    expectSetting(viewCommands(), "Session List: Show Environment", onOff);
    expectSetting(viewCommands(), "Session List: Show Repo", onOff);
    expectSetting(viewCommands(), "Session List: Show Branch", onOff);

    chooseView("Session List: Group by", "Harness");
    await waitFor(() => expect(screen.queryByTestId("project-list-actions")).toBeNull());
    expect(labels()).not.toContain("Sort Projects");
  });

  it("offers only Display without sidebar_grouping_ui, and nothing with the flag off", async () => {
    const { unmount } = renderSidebar({ features: QUICK_PICK });
    await waitFor(() =>
      expect(Object.keys(settingValues(viewCommands()))).toEqual(["Session List: Display"]),
    );
    unmount();
    usePaletteCommandsStore.setState({ groups: {} });
    renderSidebar({ features: {} });
    await screen.findByText("Alpha");
    expect(viewCommands()).toEqual([]);
  });
});
describe("the sidebar's destinations in ⌘K", () => {
  const navLabels = () =>
    usePaletteCommandsStore
      .getState()
      .groups["sidebar-nav"]?.commands.map((command) => command.label) ?? [];

  it("offers Go to Canvas and Go to Usage exactly when the sidebar links them", async () => {
    renderSidebar({ features: { ...QUICK_PICK, canvas: true, usage_page: true } });
    expect(await screen.findByTestId("canvas-nav")).toBeInTheDocument();
    expect(screen.getByTestId("usage-nav")).toBeInTheDocument();
    await waitFor(() => expect(navLabels()).toEqual(["Go to Canvas", "Go to Usage"]));
  });

  it("offers neither without their pages, nor with the palette flag off", async () => {
    const { unmount } = renderSidebar({ features: QUICK_PICK });
    await screen.findByText("Alpha");
    expect(screen.queryByTestId("canvas-nav")).toBeNull();
    expect(navLabels()).toEqual([]);
    unmount();
    renderSidebar({ features: { canvas: true, usage_page: true } });
    expect(await screen.findByTestId("canvas-nav")).toBeInTheDocument();
    expect(navLabels()).toEqual([]);
  });
});
