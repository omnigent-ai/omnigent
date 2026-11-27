// The sidebar's view, project list and selection actions in ⌘K: registered by the sidebar
// itself from the same models its headers and menus render, running the same handlers.

import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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

import { reportProjectRenamed, useProjectActionStore } from "./projectCommands/projectActionStore";
import { projectOrderingStore } from "./projectCommands/projectOrderingStore";
import { EXPANDED_PROJECT_SECTIONS_STORAGE_KEY } from "./sidebarNav";

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
  useProjectActionStore.setState({ request: null, lastRename: null });
  usePaletteCommandsStore.setState({ groups: {} });
});

const commands = (): readonly PaletteCommand[] =>
  usePaletteCommandsStore.getState().groups["sidebar-projects"]?.commands ?? [];
const labels = () => commands().map((command) => command.label);
const run = (label: string) =>
  act(() => {
    const command = commands().find((candidate) => candidate.label === label);
    if (!command) throw new Error(`No "${label}" among: ${labels().join(", ")}`);
    command.run();
  });
/** A command of the selection groups ⌘K offers while selecting. */
const selectionCommand = (label: string): PaletteCommand => {
  const found = Object.entries(usePaletteCommandsStore.getState().groups)
    .filter(([source]) => source.startsWith("sidebar.selection"))
    .flatMap(([, group]) => group.commands)
    .find((candidate) => candidate.label === label);
  if (!found) throw new Error(`No selection command "${label}"`);
  return found;
};
/** The label of the value a setting row shows as chosen. */
const settingValue = (label: string) => {
  const setting = commands().find((command) => command.label === label)?.setting;
  return setting?.choices.find((choice) => choice.value === setting.current)?.label;
};

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

describe("the project list in ⌘K", () => {
  it("shows the project sort as a setting, and Expand All while some are collapsed", async () => {
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() => expect(settingValue("Sort Projects")).toBe("Manual order"));
    expect(labels()).toContain("Expand All Projects");

    run("Expand All Projects");
    await waitFor(() => expect(labels()).toContain("Collapse All Projects"));
    expect(labels()).not.toContain("Expand All Projects");
  });

  it("registers nothing with the flag off", async () => {
    renderSidebar({ features: {} });
    await screen.findByText("Alpha");
    expect(labels()).toEqual([]);
    expect(projectOrderingStore.getState().ordering).not.toBeNull();
  });
});

describe("moving a project", () => {
  const openMove = async (projectName: string) => {
    const folder = (await screen.findByText(projectName)).closest("section")!;
    fireEvent.pointerDown(within(folder).getByTestId("project-actions"), { button: 0 });
    fireEvent.keyDown(await screen.findByTestId("move-project"), { key: "ArrowRight" });
  };

  it("publishes the order ⌘K moves the project in view by, and greys the menu's edges", async () => {
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() =>
      expect(projectOrderingStore.getState().ordering?.names).toEqual(["Alpha", "Beta"]),
    );
    await waitFor(() =>
      expect(projectOrderingStore.getState().ordering?.facts).toEqual({
        orderLoaded: true,
        saving: false,
        selecting: false,
        editing: false,
      }),
    );
    await openMove("Alpha");
    expect(await screen.findByRole("menuitem", { name: "Move up" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("menuitem", { name: "Move down" })).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("publishes that the order is still loading", async () => {
    projectOrder.settled = false;
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() =>
      expect(projectOrderingStore.getState().ordering?.facts.orderLoaded).toBe(false),
    );
  });

  it("withdraws the published order when the sidebar unmounts", async () => {
    const { unmount } = renderSidebar({ features: QUICK_PICK });
    await waitFor(() => expect(projectOrderingStore.getState().ordering).not.toBeNull());
    unmount();
    expect(projectOrderingStore.getState().ordering).toBeNull();
  });
});

describe("the entry points and ⌘K agree", () => {
  it("starts the header button's selection from Select Sessions, then withdraws the project controls", async () => {
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() => expect(labels()).toContain("Select Sessions"));
    run("Select Sessions");
    expect(await screen.findByRole("button", { name: "Exit selection mode" })).toBeInTheDocument();
    expect(screen.queryByTestId("project-list-actions")).toBeNull();
    await waitFor(() => expect(labels()).toEqual([]));
  });

  it("offers Select Sessions with an empty list, as the header's button does", async () => {
    vi.mocked(useConversations).mockImplementation(() => conversationPage([]));
    vi.mocked(useProjects).mockReturnValue({ data: [] } as unknown as ReturnType<
      typeof useProjects
    >);
    renderSidebar({ features: QUICK_PICK });
    expect(await screen.findByTestId("toggle-selection-mode")).toBeEnabled();
    await waitFor(() => expect(labels()).toContain("Select Sessions"));
  });

  it("selects project sessions when only folders hold any", async () => {
    vi.mocked(useConversations).mockImplementation(() =>
      conversationPage([
        conversation("filed1", {
          title: "Filed session",
          project_id: "project-a",
          permission_level: 4,
        }),
      ]),
    );
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() => expect(labels()).toContain("Select Sessions"));
    run("Expand All Projects");
    await screen.findByText("Filed session");
    run("Select Sessions");
    expect(await screen.findByRole("button", { name: "Exit selection mode" })).toBeInTheDocument();
    // The filed row is selectable, and Select all reaches it.
    fireEvent.click(screen.getByRole("link", { name: /Filed session/ }));
    expect(screen.getByText(/\d+ selected/).textContent).toBe("1 selected");
    fireEvent.click(screen.getByRole("link", { name: /Filed session/ }));
    expect(screen.getByText(/\d+ selected/).textContent).toBe("0 selected");
    act(() => selectionCommand("Select all sessions").run());
    expect(screen.getByText(/\d+ selected/).textContent).toBe("1 selected");
  });

  it("withdraws the project header's rows when grouping hides the Projects section", async () => {
    localStorage.setItem("omnigent:sidebar-view", JSON.stringify({ grouping: "harness" }));
    renderSidebar({ features: { ...QUICK_PICK, sidebar_grouping_ui: true } });
    await screen.findByText("Loose session");
    expect(screen.queryByTestId("project-list-actions")).toBeNull();
    expect(labels()).not.toContain("Sort Projects");
    expect(labels()).not.toContain("Expand All Projects");
  });

  it("offers no project sorting when an empty project list has no menu", async () => {
    vi.mocked(useProjects).mockReturnValue({ data: [] } as unknown as ReturnType<
      typeof useProjects
    >);
    renderSidebar({ features: QUICK_PICK });
    await waitFor(() => expect(labels()).toContain("Select Sessions"));
    expect(screen.queryByTestId("project-list-actions")).toBeNull();
    expect(labels()).not.toContain("Sort Projects");
  });
});

describe("a project renamed from ⌘K", () => {
  const expanded = () =>
    JSON.parse(localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY) ?? "[]") as string[];

  it("carries the folder's expansion over once, not again when the sidebar remounts", async () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Alpha"]));
    const first = renderSidebar({ features: QUICK_PICK });
    await screen.findByText("Alpha");
    act(() => reportProjectRenamed("Alpha", "Gamma"));
    await waitFor(() => expect(expanded()).toEqual(["Gamma"]));
    first.unmount();

    // Alpha is opened again afterwards, as a rename back through the folder menu leaves it.
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Alpha"]));
    renderSidebar({ features: QUICK_PICK });
    await screen.findByText("Alpha");
    await waitFor(() => expect(labels()).toContain("Collapse All Projects"));
    expect(expanded()).toEqual(["Alpha"]);
  });

  it("leaves the expansion alone with the flag off", async () => {
    localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(["Alpha"]));
    act(() => reportProjectRenamed("Alpha", "Gamma"));
    renderSidebar({ features: {} });
    await screen.findByText("Alpha");
    expect(expanded()).toEqual(["Alpha"]);
  });
});
