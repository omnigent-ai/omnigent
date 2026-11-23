import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import { isCommandAvailable, usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { type CommandSession, CommandContextProvider, type CommandView } from "../commandContext";
import { ProjectActionHost } from "./ProjectActionHost";
import { useProjectActionStore } from "./projectActionStore";
import { projectOrderingStore } from "./projectOrderingStore";
import { projectInView } from "./useProjectPaletteCommands";

const PAYMENTS: ProjectSummary = { id: "project-1", name: "Payments", icon: "💸" };
const LABEL_ONLY: ProjectSummary = { id: null, name: "Scratch" };

const mocks = vi.hoisted(() => ({
  projects: undefined as ProjectSummary[] | undefined,
  sessionRow: null as Partial<Conversation> | null,
  menuArgs: [] as unknown[],
  actions: { openRename: vi.fn(), openSettings: vi.fn(), openDelete: vi.fn() },
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useProjects: () => ({ data: mocks.projects }),
}));

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "viewer" }));

vi.mock("@/hooks/useOpenSessionRow", () => ({
  useOpenSessionRow: (id: string | null) => (id ? mocks.sessionRow : null),
}));

vi.mock("../Sidebar", () => ({
  useProjectFolderMenu: (...args: unknown[]) => {
    mocks.menuArgs = args;
    return { actions: mocks.actions, dialogs: <div data-testid="project-dialogs" /> };
  },
}));

const SESSION: CommandSession = {
  id: "conv-1",
  permissionLevel: null,
  permissionResolved: true,
  topLevel: true,
  archived: false,
};

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderHost({
  view,
  path = "/c/conv-1",
  flag = true,
  singleUser = false,
}: {
  view: CommandView;
  path?: string;
  flag?: boolean;
  singleUser?: boolean;
}) {
  const info = {
    ...FALLBACK_SERVER_INFO,
    single_user: singleUser,
    features: { quick_pick_palette: flag },
  };
  render(
    <CapabilitiesProvider info={info}>
      <MemoryRouter initialEntries={[path]}>
        <CommandContextProvider
          session={view === "session" ? SESSION : null}
          view={view}
          workspaceTab={null}
          filePath={null}
        >
          <ProjectActionHost />
          <LocationProbe />
        </CommandContextProvider>
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
}

const labels = () => registeredPaletteCommands().map((command) => command.label);
const run = (label: string) =>
  act(() => {
    const command = registeredPaletteCommands().find((candidate) => candidate.label === label);
    if (!command) throw new Error(`No "${label}" among: ${labels().join(", ")}`);
    command.run();
  });
// The folder menu's callbacks the host passed, as its dialogs call them once they succeed.
const onRenamed = (oldName: string, newName: string) =>
  act(() => (mocks.menuArgs[3] as (oldName: string, newName: string) => void)(oldName, newName));
const onDeleted = () => act(() => (mocks.menuArgs[4] as () => void)());

beforeEach(() => {
  mocks.projects = [PAYMENTS, LABEL_ONLY];
  mocks.sessionRow = { project_id: "project-1", labels: {}, owner: "viewer" };
  mocks.menuArgs = [];
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
  useProjectActionStore.setState({ request: null, lastRename: null });
  projectOrderingStore.setState({ ordering: null });
});

describe("moving the project in view from ⌘K", () => {
  const LOADED = { orderLoaded: true, saving: false, selecting: false, editing: false };
  /** The sidebar's project order, as it publishes it while mounted. */
  function publishOrder(names: string[], facts = LOADED) {
    const move = vi.fn();
    projectOrderingStore.setState({ ordering: { names, facts, move } });
    return move;
  }
  const command = (label: string) =>
    registeredPaletteCommands().find((candidate) => candidate.label === label);

  it("offers the folder menu's moves, running the sidebar's move", () => {
    const move = publishOrder(["Inbox", "Payments", "Zebra"]);
    renderHost({ view: "session" });
    expect(labels()).toEqual([
      "New session in Payments",
      "Project settings for Payments…",
      "Rename project Payments…",
      "Delete project Payments…",
      "Move Project Payments Up",
      "Move Project Payments Down",
      "Move Project Payments to the Top",
      "Move Project Payments to the Bottom",
    ]);
    run("Move Project Payments Up");
    expect(move).toHaveBeenCalledExactlyOnceWith("Payments", "up");
  });

  it("greys the moves past the list's edge, and every move while the order saves", () => {
    publishOrder(["Payments", "Zebra"]);
    renderHost({ view: "session" });
    expect(isCommandAvailable(command("Move Project Payments Up")!.availability)).toBe(false);
    expect(command("Move Project Payments Down")!.availability).toEqual({ available: true });

    act(() => {
      publishOrder(["Payments", "Zebra"], { ...LOADED, saving: true });
    });
    expect(command("Move Project Payments Down")!.availability).toEqual({
      available: false,
      reason: "Saving the project order…",
    });
  });

  it("offers no moves while no sidebar lists the project", () => {
    renderHost({ view: "session" });
    expect(labels().filter((label) => label.startsWith("Move"))).toEqual([]);
    act(() => {
      publishOrder(["Inbox"]);
    });
    expect(labels().filter((label) => label.startsWith("Move"))).toEqual([]);
  });
});

describe("the project in view's ⌘K commands", () => {
  it("names the open session's project and opens its folder menu's dialog", () => {
    renderHost({ view: "session" });
    expect(labels()).toEqual([
      "New session in Payments",
      "Project settings for Payments…",
      "Rename project Payments…",
      "Delete project Payments…",
    ]);
    expect(registeredPaletteCommands().every((command) => command.movesFocus)).toBe(true);
    run("Rename project Payments…");
    expect(mocks.menuArgs.slice(0, 3)).toEqual(["Payments", "project-1", "💸"]);
    expect(mocks.actions.openRename).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("project-dialogs")).toBeInTheDocument();
  });

  it("opens the same action again when it's asked for twice", () => {
    renderHost({ view: "session" });
    run("Delete project Payments…");
    run("Delete project Payments…");
    expect(mocks.actions.openDelete).toHaveBeenCalledTimes(2);
    run("Project settings for Payments…");
    expect(mocks.actions.openSettings).toHaveBeenCalledTimes(1);
  });

  it("starts a new session in the project", () => {
    renderHost({ view: "session" });
    run("New session in Payments");
    expect(screen.getByTestId("location")).toHaveTextContent("/?project=Payments");
  });

  it("offers nothing for a session in a project the viewer doesn't have", () => {
    mocks.sessionRow = { project_id: "someone-elses", labels: {} };
    renderHost({ view: "session" });
    expect(labels()).toEqual([]);
  });

  it("matches a session's project label only when the session is the viewer's", () => {
    const labelled = { project_id: null, labels: { [PROJECT_LABEL_KEY]: "Payments" } };
    // A shared session from someone else whose own folder happens to share the name.
    mocks.sessionRow = { ...labelled, owner: "someone-else" };
    renderHost({ view: "session" });
    expect(labels()).toEqual([]);
    cleanup();

    mocks.sessionRow = { ...labelled, owner: "viewer" };
    renderHost({ view: "session" });
    expect(labels()).toContain("Rename project Payments…");
    cleanup();

    // The single-user local runtime owns every session.
    mocks.sessionRow = { ...labelled, owner: null, permission_level: null };
    renderHost({ view: "session", singleUser: true });
    expect(labels()).toContain("Rename project Payments…");
  });

  it("follows a rename in the new-session view filed under the project", () => {
    renderHost({ view: "new-session", path: "/?project=Payments" });
    expect(labels()).not.toContain("New session in Payments");
    run("Rename project Payments…");
    onRenamed("Payments", "Billing");
    expect(screen.getByTestId("location")).toHaveTextContent("/?project=Billing");
    expect(useProjectActionStore.getState().lastRename).toMatchObject({
      oldName: "Payments",
      newName: "Billing",
    });
  });

  it("leaves the new-session view filed under a project once it's deleted", () => {
    renderHost({ view: "new-session", path: "/?project=Payments" });
    run("Delete project Payments…");
    onDeleted();
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
  });

  it("leaves the session view where it is after a rename or delete", () => {
    renderHost({ view: "session" });
    run("Delete project Payments…");
    onRenamed("Payments", "Billing");
    onDeleted();
    expect(screen.getByTestId("location")).toHaveTextContent("/c/conv-1");
  });

  it("offers nothing with the flag off", () => {
    renderHost({ view: "session", flag: false });
    expect(labels()).toEqual([]);
  });
});

describe("projectInView", () => {
  const base = { newSessionProject: null, ownSession: true, projects: [PAYMENTS, LABEL_ONLY] };

  it("finds a session's label-only folder by its label", () => {
    const session = { project_id: null, labels: { [PROJECT_LABEL_KEY]: "Scratch" } };
    expect(projectInView({ ...base, view: "session", session })).toBe(LABEL_ONLY);
    expect(projectInView({ ...base, ownSession: false, view: "session", session })).toBe(null);
  });

  it("finds nothing while the project list loads, or for an unfiled session", () => {
    const filed = { project_id: "project-1", labels: {} };
    expect(projectInView({ ...base, projects: undefined, view: "session", session: filed })).toBe(
      null,
    );
    expect(
      projectInView({ ...base, view: "session", session: { project_id: null, labels: {} } }),
    ).toBe(null);
  });

  it("reads the new-session view's project and ignores other views", () => {
    expect(
      projectInView({ ...base, view: "new-session", session: null, newSessionProject: "Payments" }),
    ).toBe(PAYMENTS);
    expect(
      projectInView({ ...base, view: "new-session", session: null, newSessionProject: "Gone" }),
    ).toBe(null);
    expect(
      projectInView({ ...base, view: "inbox", session: { project_id: "project-1", labels: {} } }),
    ).toBe(null);
  });
});
