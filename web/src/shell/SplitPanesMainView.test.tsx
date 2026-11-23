import type * as CapabilitiesModule from "@/lib/capabilities";
import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FolderTreeIcon } from "lucide-react";
import { useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TerminalInfo } from "@/hooks/useTerminals";
import { terminalLayoutKeys, type TerminalSplitState } from "@/lib/paneSplitLayout";
import { resetPanePaletteOwnerForTests } from "@/hooks/useTerminalPaneHotkeys";
import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { resetAppPanes, setAppPaneTabActions, setAppPaneTitle } from "./appPanes";
import { ChatPaneBody } from "./ChatPaneBody";
import { readPaneSplit, resetPaneSplitsCache, updatePaneSplit } from "./paneSplitStore";
import { SidebarTerminalPanes } from "./SidebarTerminalPanes";
import { SplitPanesMainView, type SplitPanesMainViewProps } from "./SplitPanesMainView";
import type { TerminalPaneSurfaceProps } from "./TerminalPaneSurface";
import {
  pendingTerminalFocus,
  requestTerminalFocus,
  settleTerminalFocus,
} from "./terminalFocusRequest";
import { resetTerminalOpenRequestsForTests, useTerminalOpenRequests } from "./terminalOpenRequest";
import { resetTerminalPlacement, useMainOwnedTerminalKeys } from "./terminalPlacement";

const AGENT_ID = "terminal_claude_main";
const SHELL_ID = "terminal_zsh_s1";
const AGENT = `terminal:${AGENT_ID}`;
const SHELL = `terminal:${SHELL_ID}`;
const OTHER_ID = "terminal_zsh_s2";
const OTHER = `terminal:${OTHER_ID}`;

const listed = vi.hoisted(() => ({
  terminals: [] as TerminalInfo[],
  listCompleteAt: 1,
  hostOnline: true as boolean | null,
  splitsAllowed: true,
  launch: vi.fn(),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: () => ({
    terminals: listed.terminals,
    listCompleteAt: listed.listCompleteAt,
    isFetching: false,
  }),
  useCreateTerminal: () => ({ launch: listed.launch, isPending: false }),
  useDeleteTerminal: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionHostOnline: () => listed.hostOnline,
}));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: () => ({ data: { terminals: ["zsh"] } }) }));
vi.mock("./useTerminalStatuses", () => ({
  useTerminalStatuses: () => ({ setTerminalConnectionState: vi.fn(), markTerminalActive: vi.fn() }),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  isFeatureEnabled: () => listed.splitsAllowed,
}));
// The Terminal view stands in for every state with nothing to show as panes.
vi.mock("./MainTerminalView", () => ({
  MainTerminalView: (props: { splittable?: boolean; initialTerminalKey?: string | null }) => (
    <div data-testid="main-terminal-view" data-splittable={String(props.splittable ?? false)} />
  ),
}));
const surfaces = vi.hoisted(() => new Map<string, { onResume?: () => void | Promise<void> }>());
vi.mock("./TerminalPaneSurface", () => ({
  TerminalPaneSurface: (props: TerminalPaneSurfaceProps) => {
    const { terminal, active, focusRequest } = props;
    useEffect(() => {
      surfaces.set(terminal.id, props);
    });
    return (
      <div
        data-testid={`terminal-${terminal.id}`}
        data-active={String(active)}
        data-focus-request={focusRequest ?? ""}
      >
        <div {...{ [TERMINAL_SURFACE_ATTRIBUTE]: "" }}>
          <textarea aria-label={`${terminal.id} terminal`} />
        </div>
      </div>
    );
  },
}));

const TERMINALS: TerminalInfo[] = [
  { id: AGENT_ID, name: "claude", session: "main", running: true },
  { id: SHELL_ID, name: "zsh", session: "s1", running: true },
];
const SIDE_BY_SIDE: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
  focusedKey: SHELL,
};

let conversation = 0;
const nextConversation = () => `conv_split_panes_main_${++conversation}`;

function View(props: Partial<SplitPanesMainViewProps> & { conversationId: string }) {
  return <SplitPanesMainView visible readOnly={false} {...props} />;
}

const paneCommandLabels = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((command) => command.label),
  );

beforeEach(() => {
  listed.terminals = TERMINALS;
  listed.listCompleteAt = 1;
  listed.hostOnline = true;
  listed.splitsAllowed = true;
  listed.launch.mockClear();
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
  resetPaneSplitsCache();
  resetAppPanes();
  resetTerminalPlacement();
  resetTerminalOpenRequestsForTests();
  resetPanePaletteOwnerForTests();
  surfaces.clear();
});

describe("SplitPanesMainView", () => {
  it.each([
    ["a viewer who can't split", { readOnly: true }],
    ["a shell the sidebar opened", { initialTerminalKey: SHELL }],
    ["a stopped sandbox", { sandboxOffline: true }],
  ])("leaves %s to the Terminal view, unsplit", (_case, props) => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    render(<View conversationId={conversationId} {...props} />);
    expect(screen.getByTestId("main-terminal-view").dataset.splittable).toBe("false");
    expect(screen.queryByTestId("split-panes-main-view")).toBeNull();
  });

  it("keeps the Chat/Terminal switch in the header until a split puts the chat in a pane", async () => {
    const conversationId = nextConversation();
    const toggleHosts = () => document.querySelectorAll("[data-pane-strip-view-toggle-host]");
    const { rerender } = render(<View conversationId={conversationId} />);
    expect(await screen.findByTestId(`terminal-${AGENT_ID}`)).toBeInTheDocument();
    // Picking Terminal mustn't move the switch from under the pointer.
    expect(toggleHosts()).toHaveLength(0);

    act(() => updatePaneSplit(conversationId, () => SIDE_BY_SIDE));
    rerender(<View conversationId={conversationId} />);
    expect(toggleHosts()).toHaveLength(1);
  });

  it("keeps a saved split up while the list briefly lacks the agent's terminal", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    const { rerender } = render(<View conversationId={conversationId} />);
    expect(await screen.findByTestId("split-panes-main-view")).toBeInTheDocument();

    // A refresh lists only the shell for a moment; the split and a drag in it stay up.
    listed.terminals = [TERMINALS[1]!];
    rerender(<View conversationId={conversationId} />);
    expect(screen.getByTestId("split-panes-main-view")).toBeInTheDocument();

    // An empty list shows no panes.
    listed.terminals = [];
    rerender(<View conversationId={conversationId} />);
    expect(screen.queryByTestId("split-panes-main-view")).toBeNull();
  });

  it("leaves the Terminal view to say why while the agent's terminal isn't listed", () => {
    listed.terminals = [TERMINALS[1]!];
    render(<View conversationId={nextConversation()} />);
    expect(screen.getByTestId("main-terminal-view")).toBeInTheDocument();
  });

  it("shows the agent's terminal as its own pane, and the saved split beside it", async () => {
    const conversationId = nextConversation();
    const { rerender } = render(<View conversationId={conversationId} />);
    expect((await screen.findByTestId(`terminal-${AGENT_ID}`)).dataset.active).toBe("true");
    expect(screen.queryByTestId(`terminal-${SHELL_ID}`)).toBeNull();

    act(() => updatePaneSplit(conversationId, () => SIDE_BY_SIDE));
    rerender(<View conversationId={conversationId} />);
    expect(screen.getByTestId(`terminal-${SHELL_ID}`).dataset.active).toBe("true");
    expect(screen.getByTestId(`terminal-${AGENT_ID}`).dataset.active).toBe("false");
  });

  it("tells the sidebar which shells it shows, so they never mount there too", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    let owned: ReadonlySet<string> = new Set();
    function Sidebar() {
      owned = useMainOwnedTerminalKeys(conversationId, { splitMembers: new Set() });
      return null;
    }
    render(
      <>
        <View conversationId={conversationId} />
        <Sidebar />
      </>,
    );
    await screen.findByTestId(`terminal-${SHELL_ID}`);
    expect([...owned].sort()).toEqual([AGENT, SHELL].sort());
  });

  it("drops a shell that ended from the split, and keeps a workspace tab shown as a pane", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: [AGENT, { axis: "rows", ratio: 0.5, children: [SHELL, "app:files"] }],
      },
      focusedKey: SHELL,
    }));
    listed.terminals = [TERMINALS[0]!];
    render(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(readPaneSplit(conversationId)?.layout).toEqual({
      axis: "columns",
      ratio: 0.5,
      children: [AGENT, "app:files"],
    });
  });

  it("keeps a live shell in the split when the list refills after an empty one, before a new answer", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    const view = render(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${SHELL_ID}`);
    // The list empties, as on an agent switch, and refills one terminal at a time while the
    // last complete answer is still the one from before.
    listed.terminals = [];
    view.rerender(<View conversationId={conversationId} />);
    listed.terminals = [TERMINALS[0]!];
    view.rerender(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(readPaneSplit(conversationId)?.layout).toEqual(SIDE_BY_SIDE.layout);
  });

  it("recovers a missing agent beside a live shell, which stays mounted, and offers Resume if that fails", async () => {
    let failResume!: (error: Error) => void;
    const onResume = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          failResume = reject;
        }),
    );
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    const view = render(<View conversationId={conversationId} onResume={onResume} />);
    const shell = await screen.findByTestId(`terminal-${SHELL_ID}`);

    listed.terminals = [TERMINALS[1]!];
    view.rerender(<View conversationId={conversationId} onResume={onResume} />);
    expect(await screen.findByRole("status")).toHaveTextContent("Reconnecting…");
    expect(onResume).toHaveBeenCalledTimes(1);
    await act(async () => failResume(new Error("unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn't reconnect the terminal automatically.",
    );
    expect(screen.getByTestId(`terminal-${SHELL_ID}`)).toBe(shell);

    onResume.mockResolvedValueOnce(undefined);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Resume session" })));
    expect(onResume).toHaveBeenCalledTimes(2);
    listed.terminals = TERMINALS;
    view.rerender(<View conversationId={conversationId} onResume={onResume} />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(screen.getByTestId(`terminal-${SHELL_ID}`)).toBe(shell);
    expect(readPaneSplit(conversationId)?.layout).toEqual(SIDE_BY_SIDE.layout);
  });

  it("gives the pane beside a shell that ended the keyboard, if the shell's pane had it", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    const view = render(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${SHELL_ID}`);
    act(() => screen.getByLabelText(`${SHELL_ID} terminal`).focus());
    // The shell exits on its own: its row leaves the list.
    listed.terminals = [TERMINALS[0]!];
    view.rerender(<View conversationId={conversationId} />);
    expect(readPaneSplit(conversationId)).toBeUndefined();
    expect(screen.getByTestId(`terminal-${AGENT_ID}`).dataset.focusRequest).not.toBe("");
  });

  it("leaves the keyboard where it is when a shell ends while its pane didn't have it", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: {
        axis: "columns",
        ratio: 0.5,
        children: [AGENT, { axis: "rows", ratio: 0.5, children: [SHELL, OTHER] }],
      },
      focusedKey: SHELL,
    }));
    listed.terminals = [...TERMINALS, { id: OTHER_ID, name: "zsh", session: "s2", running: true }];
    const outside = document.body.appendChild(document.createElement("input"));
    try {
      const view = render(<View conversationId={conversationId} />);
      await screen.findByTestId(`terminal-${SHELL_ID}`);
      act(() => outside.focus());
      listed.terminals = [TERMINALS[0]!, listed.terminals[2]!];
      view.rerender(<View conversationId={conversationId} />);
      expect(terminalLayoutKeys(readPaneSplit(conversationId)!.layout)).toEqual([AGENT, OTHER]);
      expect(document.activeElement).toBe(outside);
      expect(screen.getByTestId(`terminal-${AGENT_ID}`).dataset.focusRequest).toBe("");
      expect(screen.getByTestId(`terminal-${OTHER_ID}`).dataset.focusRequest).toBe("");
    } finally {
      outside.remove();
    }
  });

  it("hosts a workspace tab as a pane, its row titled as its tab", async () => {
    const conversationId = nextConversation();
    setAppPaneTabActions(conversationId, { close: vi.fn(), moveToSidebar: vi.fn() });
    setAppPaneTitle(conversationId, "files", { label: "Files", icon: FolderTreeIcon });
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, "app:files"] },
      focusedKey: "app:files",
    }));
    render(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(document.querySelector('[data-app-pane-host="files"]')).not.toBeNull();
    expect(document.querySelector('[data-pane-strip="app:files"]')?.textContent).toContain("Files");
  });

  it("renders the web chat as the agent's pane while the session shows Chat beside a split", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({ ...SIDE_BY_SIDE, focusedKey: AGENT }));
    render(<View conversationId={conversationId} chatShown />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(document.querySelector("[data-chat-pane-body-host]")).not.toBeNull();
    expect(document.querySelector(`[data-pane-strip="${AGENT}"]`)?.textContent).toContain("Chat");
    // Under the web chat, the agent's terminal keeps no keyboard.
    expect(screen.getByTestId(`terminal-${AGENT_ID}`).dataset.active).toBe("false");
  });

  it("holds a focus request for the chat pane until its composer is there to take it", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({ ...SIDE_BY_SIDE, focusedKey: AGENT }));
    render(<View conversationId={conversationId} chatShown />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    const host = document.querySelector<HTMLElement>("[data-chat-pane-body-host]")!;
    act(() => requestTerminalFocus({ conversationId, surface: "main", tabKey: null }));
    // The chat hasn't been portaled in yet: the request waits instead of being dropped.
    expect(pendingTerminalFocus(conversationId, "main")).not.toBeNull();

    const body = document.createElement("div");
    body.setAttribute("data-chat-pane-body", conversationId);
    body.innerHTML = `<textarea aria-label="Message"></textarea>`;
    await act(async () => {
      host.append(body);
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(screen.getByLabelText("Message"));
    expect(pendingTerminalFocus(conversationId, "main")).toBeNull();
  });

  it.each([
    ["⌘D", false],
    ["⇧⌘D", true],
  ])(
    "splits the agent's pane with %s from the composer of a chat not split yet",
    async (_chord, shiftKey) => {
      const conversationId = nextConversation();
      render(
        <>
          <View conversationId={conversationId} visible={false} chatShown />
          <ChatPaneBody sessionId={conversationId} host={null} marked>
            <textarea aria-label="composer" />
          </ChatPaneBody>
        </>,
      );
      await screen.findByTestId(`terminal-${AGENT_ID}`);
      const composer = screen.getByLabelText("composer");
      act(() => composer.focus());
      fireEvent.keyDown(composer, { key: "d", code: "KeyD", metaKey: true, shiftKey });
      expect(listed.launch).toHaveBeenCalledOnce();
    },
  );

  it("hands a focus request for the view to the split's focused pane", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    render(<View conversationId={conversationId} />);
    await screen.findByTestId(`terminal-${SHELL_ID}`);
    act(() => requestTerminalFocus({ conversationId, surface: "main", tabKey: null }));
    expect(screen.getByTestId(`terminal-${SHELL_ID}`).dataset.focusRequest).not.toBe("");
    expect(screen.getByTestId(`terminal-${AGENT_ID}`).dataset.focusRequest).toBe("");
    act(() => settleTerminalFocus(conversationId, "main"));
  });

  it("lists Move Pane to Sidebar for a focused shell, which moves it without ending it", async () => {
    const conversationId = nextConversation();
    const opened = vi.fn();
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    function Opener() {
      useTerminalOpenRequests(conversationId, opened);
      return null;
    }
    render(
      <>
        <View conversationId={conversationId} />
        <Opener />
      </>,
    );
    await screen.findByTestId(`terminal-${SHELL_ID}`);
    const command = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((candidate) => candidate.label === "Move Pane to Sidebar");
    act(() => command!.run());
    expect(readPaneSplit(conversationId)).toBeUndefined();
    expect(opened).toHaveBeenCalledExactlyOnceWith({ key: SHELL, focus: true });
    // The agent's own pane can't move, so the command leaves with the split.
    expect(paneCommandLabels()).not.toContain("Move Pane to Sidebar");
  });

  it("lists Move Pane to Sidebar only while its panes last had the keyboard", async () => {
    const conversationId = nextConversation();
    listed.terminals = [...TERMINALS, { id: OTHER_ID, name: "zsh", session: "s2", running: true }];
    updatePaneSplit(conversationId, () => SIDE_BY_SIDE);
    render(
      <>
        <View conversationId={conversationId} />
        <SidebarTerminalPanes
          conversationId={conversationId}
          shownKey={OTHER}
          mountedKeys={[OTHER]}
          readOnly={false}
          visible
          autoFocusKey={null}
        />
      </>,
    );
    await screen.findByTestId(`terminal-${OTHER_ID}`);
    expect(paneCommandLabels()).toContain("Move Pane to Sidebar");
    act(() => screen.getByLabelText(`${OTHER_ID} terminal`).focus());
    expect(paneCommandLabels()).not.toContain("Move Pane to Sidebar");
    act(() => screen.getByLabelText(`${SHELL_ID} terminal`).focus());
    expect(paneCommandLabels()).toContain("Move Pane to Sidebar");
  });

  it("offers the terminal's own Resume while the runner is offline", async () => {
    const onResume = vi.fn();
    const conversationId = nextConversation();
    const { rerender } = render(<View conversationId={conversationId} onResume={onResume} />);
    await screen.findByTestId(`terminal-${AGENT_ID}`);
    expect(surfaces.get(AGENT_ID)?.onResume).toBeUndefined();
    rerender(<View conversationId={conversationId} onResume={onResume} runnerOnline={false} />);
    expect(surfaces.get(AGENT_ID)?.onResume).toBe(onResume);
  });
});
