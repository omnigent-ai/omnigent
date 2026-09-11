import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UseDraftWorkspaceResult } from "@/hooks/useDraftWorkspace";
import type { LandingWorkspaceSelection } from "@/lib/landingWorkspaceState";
import { LandingWorkspacePanel } from "./LandingWorkspacePanel";

const mocks = vi.hoisted(() => ({
  state: {
    browserNamespace: "draft-workspace:browser-1",
    selection: null as LandingWorkspaceSelection | null,
    panel: {} as Record<string, unknown>,
    starting: false,
    busy: false,
  },
  writePanel: vi.fn(),
  changedFiles: vi.fn(() => ({ data: { data: [] as unknown[] } })),
  supportsBrowser: vi.fn(() => true),
  workspaceProps: null as Record<string, unknown> | null,
  openBrowser: vi.fn(),
}));

vi.mock("@/lib/landingWorkspaceState", () => ({
  useLandingWorkspaceState: () => mocks.state,
  landingContextClaimedElsewhere: () => false,
  readLandingWorkspaceState: () => mocks.state,
  writeLandingWorkspacePanel: mocks.writePanel,
  landingResourceTarget: (selection: LandingWorkspaceSelection | null) =>
    selection?.available && selection.hostId
      ? { kind: "host", hostId: selection.hostId, workspace: selection.workspace }
      : undefined,
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: mocks.changedFiles,
}));
vi.mock("@/lib/nativeBridge", () => ({ supportsBrowser: mocks.supportsBrowser }));
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: () => <div data-testid="draft-terminal" />,
}));
vi.mock("./WorkspacePanel", () => ({
  WorkspacePanel: (props: Record<string, unknown>) => {
    mocks.workspaceProps = props;
    const renderNewTabMenu = props.renderDraftNewTabMenu as
      ((onOpenBrowser: () => void) => React.ReactNode) | undefined;
    return <div data-testid="workspace-panel">{renderNewTabMenu?.(mocks.openBrowser)}</div>;
  },
}));

function terminal(id: string) {
  return { id, name: "bash", session: "draft", running: true };
}

function draft(overrides: Partial<UseDraftWorkspaceResult> = {}): UseDraftWorkspaceResult {
  return {
    context: null,
    terminals: [],
    isLoading: false,
    error: null,
    ensureContext: vi.fn().mockResolvedValue({
      id: "context-1",
      hostId: "host-1",
      workspace: "/repo",
      workspaceAliases: ["/repo"],
      session_id: null,
      lease_seconds: 60,
    }),
    refreshTerminals: vi.fn().mockResolvedValue([]),
    createTerminal: vi.fn().mockResolvedValue(terminal("term-1")),
    deleteTerminal: vi.fn().mockResolvedValue(undefined),
    discard: vi.fn().mockResolvedValue(undefined),
    adopt: vi.fn(),
    ...overrides,
  };
}

function renderLanding(draftWorkspace = draft()) {
  render(
    <LandingWorkspacePanel
      width={420}
      handleProps={{ tabIndex: 0 }}
      maximized={false}
      onToggleMaximized={vi.fn()}
      draft={draftWorkspace}
    />,
  );
  return draftWorkspace;
}

function workspaceProps() {
  if (mocks.workspaceProps === null) throw new Error("WorkspacePanel was not rendered");
  return mocks.workspaceProps;
}

beforeEach(() => {
  Object.defineProperty(window, "omnigentDesktop", {
    configurable: true,
    value: { browserAdoptDraft: vi.fn() },
  });
  mocks.state.browserNamespace = "draft-workspace:browser-1";
  mocks.state.selection = {
    hostId: "host-1",
    workspace: "/repo",
    available: true,
    reason: "",
  };
  mocks.state.panel = {};
  mocks.state.starting = false;
  mocks.state.busy = false;
  mocks.workspaceProps = null;
  mocks.changedFiles.mockReturnValue({ data: { data: [] } });
  mocks.supportsBrowser.mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("LandingWorkspacePanel", () => {
  it("opens the selected host folder directly without creating a session or agent surface", () => {
    mocks.changedFiles.mockReturnValue({
      data: { data: [{ path: "changed.ts" }, { path: "new.ts" }] },
    });

    renderLanding();

    const target = { kind: "host", hostId: "host-1", workspace: "/repo" };
    expect(mocks.changedFiles).toHaveBeenCalledWith(target);
    expect(workspaceProps()).toMatchObject({
      landing: true,
      target,
      browserNamespace: "draft-workspace:browser-1",
      showFilesPanel: true,
      showGithubTab: true,
      showBrowserTab: true,
      changedCount: 2,
      subagentsWorking: 0,
      agentCount: 0,
      rootSessionId: null,
      permissionLevel: 3,
    });
    expect(workspaceProps()).not.toHaveProperty("conversationId");
  });

  it("does not browse the source folder when Start will create a future worktree", () => {
    mocks.state.selection = {
      hostId: "host-1",
      workspace: "/repo",
      available: false,
      reason: "The new worktree becomes available after Start.",
    };

    renderLanding();

    expect(mocks.changedFiles).toHaveBeenCalledWith(undefined);
    expect(workspaceProps()).toMatchObject({
      landing: true,
      target: undefined,
      showGithubTab: false,
      unavailableReason: "The new worktree becomes available after Start.",
    });
  });

  it("creates a draft shell lazily after ensuring the selected workspace context", async () => {
    let finishEnsure!: () => void;
    const ensureContext = vi.fn(
      () =>
        new Promise<Awaited<ReturnType<UseDraftWorkspaceResult["ensureContext"]>>>((resolve) => {
          finishEnsure = () =>
            resolve({
              id: "context-1",
              hostId: "host-1",
              workspace: "/repo",
              workspaceAliases: ["/repo"],
              session_id: null,
              lease_seconds: 60,
            });
        }),
    );
    const createTerminal = vi.fn().mockResolvedValue(terminal("term-created"));
    renderLanding(draft({ ensureContext, createTerminal }));

    expect(ensureContext).not.toHaveBeenCalled();
    expect(createTerminal).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Shell (bash)" }));

    expect(ensureContext).toHaveBeenCalledWith("host-1", "/repo");
    expect(createTerminal).not.toHaveBeenCalled();
    finishEnsure();

    await waitFor(() => expect(createTerminal).toHaveBeenCalledOnce());
    expect(mocks.writePanel).toHaveBeenCalledWith({
      selectedTerminalKey: "terminal:term-created",
      selectedFilePath: null,
    });
  });

  it("passes the landing browser action through the manual new-tab menu", async () => {
    renderLanding();

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Browser" }));

    expect(mocks.openBrowser).toHaveBeenCalledOnce();
    expect(workspaceProps()).toMatchObject({
      landing: true,
      browserNamespace: "draft-workspace:browser-1",
    });
  });

  it("disables new shells and browsers while Start is in flight", async () => {
    mocks.state.starting = true;
    mocks.state.panel = { selectedTerminalKey: "terminal:existing" };
    const ensureContext = vi.fn();
    const createTerminal = vi.fn();
    renderLanding(
      draft({
        context: {
          id: "context-1",
          hostId: "host-1",
          workspace: "/repo",
          workspaceAliases: ["/repo"],
          session_id: null,
          lease_seconds: 60,
        },
        terminals: [terminal("existing")],
        ensureContext,
        createTerminal,
      }),
    );

    expect(workspaceProps()).toMatchObject({
      resourceCreationDisabled: true,
      openTerminals: ["terminal:existing"],
      selectedTerminalKey: "terminal:existing",
    });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shell = await screen.findByRole("menuitem", { name: "Shell (bash)" });
    const browser = screen.getByRole("menuitem", { name: "Browser" });
    expect(shell).toHaveAttribute("aria-disabled", "true");
    expect(browser).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(shell);
    fireEvent.click(browser);
    expect(ensureContext).not.toHaveBeenCalled();
    expect(createTerminal).not.toHaveBeenCalled();
    expect(mocks.openBrowser).not.toHaveBeenCalled();
  });
});

describe("draft shell target isolation", () => {
  it("hides shells from a previous workspace while its cleanup is pending", () => {
    renderLanding(
      draft({
        context: {
          id: "old-context",
          hostId: "host-1",
          workspace: "/old",
          workspaceAliases: ["/old"],
          session_id: null,
          lease_seconds: 600,
        },
        terminals: [terminal("old-shell")],
      }),
    );
    expect(workspaceProps().openTerminals).toEqual([]);
    expect(workspaceProps().draftTerminals).toEqual([]);
  });
});
