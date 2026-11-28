// A shell moving between the session's panes and the workspace sidebar is the same terminal on
// one side at a time. These render both sides with the real terminal view over a stand-in
// session that records each attach, and check that the side the shell leaves has disposed its
// session before the side it joins attaches: a shell's PID and output can't tell two clients
// on one tmux session apart, so the sessions themselves are counted.

import type * as CapabilitiesModule from "@/lib/capabilities";
import type * as TerminalSessionModule from "@/components/blocks/TerminalSession";
import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, cleanup, render, screen } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import type { TerminalInfo } from "@/hooks/useTerminals";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { terminalLayoutKeys } from "@/lib/paneSplitLayout";

import { SidebarTerminalPanes } from "./SidebarTerminalPanes";
import { SplitPanesMainView } from "./SplitPanesMainView";
import { pendingTerminalFocus } from "./terminalFocusRequest";
import { resetTerminalOpenRequestsForTests, useTerminalOpenRequests } from "./terminalOpenRequest";
import {
  moveTerminalToSidebar,
  openTerminalAsPane,
  type TerminalMoveScope,
} from "./terminalPaneMoves";
import { resetTerminalPlacement } from "./terminalPlacement";
import { readPaneSplit, updatePaneSplit } from "./paneSplitStore";
import { resetTerminalGroupsCache } from "./terminalGroupsStore";

const AGENT_ID = "terminal_claude_main";
const SHELL_ID = "terminal_zsh_s1";
const AGENT = `terminal:${AGENT_ID}`;
const SHELL = `terminal:${SHELL_ID}`;

/** Each session the views attach, in order, and what the shell's ever saw at once. */
const sessions = vi.hoisted(() => ({
  log: [] as { event: "attach" | "dispose"; terminalId: string; instance: number }[],
  live: new Map<number, { terminalId: string; onState: (state: unknown) => void }>(),
  /** The most sessions one terminal had attached at once. */
  peak: new Map<string, number>(),
  next: 0,
}));

const terminalIdOf = (url: string) => /\/terminals\/([^/?]+)\/attach/.exec(url)?.[1] ?? url;
const liveFor = (terminalId: string) =>
  [...sessions.live.values()].filter((entry) => entry.terminalId === terminalId).length;

vi.mock("@/components/blocks/TerminalSession", async (importOriginal) => ({
  // Only the session class is replaced: it attaches nothing and records its life.
  ...(await importOriginal<typeof TerminalSessionModule>()),
  TerminalSession: class {
    readonly instance: number;
    readonly terminalId: string;
    disposed = false;

    constructor(_mount: HTMLElement, url: string, onState: (state: unknown) => void) {
      sessions.next += 1;
      this.instance = sessions.next;
      this.terminalId = terminalIdOf(url);
      sessions.live.set(this.instance, { terminalId: this.terminalId, onState });
      sessions.log.push({ event: "attach", terminalId: this.terminalId, instance: this.instance });
      const now = liveFor(this.terminalId);
      sessions.peak.set(this.terminalId, Math.max(sessions.peak.get(this.terminalId) ?? 0, now));
    }

    dispose() {
      if (this.disposed) return;
      this.disposed = true;
      sessions.live.delete(this.instance);
      sessions.log.push({ event: "dispose", terminalId: this.terminalId, instance: this.instance });
    }

    focus() {}
    setTheme() {}
    setFont() {}
    setClipboardEnabled() {}
    pasteCommand() {
      return () => {};
    }
  },
}));

// Creating or closing a terminal: a move must never do either.
const terminalMutations = vi.hoisted(() => ({
  create: vi.fn(),
  remove: vi.fn(),
}));
const listed = vi.hoisted(() => ({ terminals: [] as TerminalInfo[] }));
// What the sidebar is asked to open, as AppShell would answer by selecting the shell's tab.
const openRequests = vi.hoisted(() => [] as unknown[]);
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: () => ({
    terminals: listed.terminals,
    isLoading: false,
    isFetching: false,
    listCompleteAt: 1,
    error: null,
  }),
  useCreateTerminal: () => ({ launch: terminalMutations.create, isPending: false }),
  useDeleteTerminal: () => ({ mutate: terminalMutations.remove, isPending: false }),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: ["zsh"] } }),
}));
// The connection each view reports for its shell, as the tab's status shows it.
const statuses = vi.hoisted(() => ({ reported: [] as [string, unknown][] }));
vi.mock("./useTerminalStatuses", () => ({
  useTerminalStatuses: () => ({
    setTerminalConnectionState: (terminalId: string, state: unknown) =>
      statuses.reported.push([terminalId, state]),
    markTerminalActive: vi.fn(),
  }),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => false }));
vi.mock("@/lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  // Gadgets keep sessions on the classic sidebar, which this suite does not cover.
  isFeatureEnabled: (_info: unknown, key: string) => key !== "gadgets",
}));

const TERMINALS: TerminalInfo[] = [
  { id: AGENT_ID, name: "claude", session: "main", running: true },
  { id: SHELL_ID, name: "zsh", session: "s1", running: true },
];

const SCOPE: TerminalMoveScope = {
  agentKey: AGENT,
  splitsAllowed: true,
  isLive: (key) => key === AGENT || key === SHELL,
};

let conversation = 0;
const nextConversation = () => `conv_shell_moves_${++conversation}`;

/**
 * The session page as the panes view shows it: the chat's pane beside its split, and
 * the sidebar keeping the shell mounted unless the split owns it, as its shell tab would.
 */
interface SessionProps {
  conversationId: string;
  initialTerminalKey?: string | null;
  /** The session shows its Terminal view, rather than another tab of the page. */
  visible?: boolean;
  /** The person can't split the main view, which then shows the Terminal view unsplit. */
  readOnly?: boolean;
}

function Session({
  conversationId,
  initialTerminalKey = null,
  visible = true,
  readOnly = false,
}: SessionProps) {
  // The sidebar's own opener, which AppShell answers by selecting the shell's tab.
  useTerminalOpenRequests(conversationId, (request) => openRequests.push(request));
  return (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { splittable_terminal_view_ui: true } }}
    >
      <>
        <SplitPanesMainView
          conversationId={conversationId}
          initialTerminalKey={initialTerminalKey}
          visible={visible}
          readOnly={readOnly}
          chatShown
        />
        <aside aria-label="Workspace">
          <SidebarTerminalPanes
            conversationId={conversationId}
            shownKey={SHELL}
            mountedKeys={[SHELL]}
            readOnly={false}
            visible
            autoFocusKey={null}
          />
        </aside>
      </>
    </CapabilitiesProvider>
  );
}

/** Let every microtask and timer the attaches and disposals queued run. */
async function settle() {
  for (let pass = 0; pass < 5; pass += 1) {
    // oxlint-disable-next-line no-await-in-loop -- each pass lets the last one's queue drain
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  }
}

const shellEvents = () =>
  sessions.log
    .filter((entry) => entry.terminalId === SHELL_ID)
    .map(({ event, instance }) => `${event} ${instance}`);

/** The shell's session ``from`` was disposed, and a new one attached after it is the live one. */
function expectHandedOver(from: number) {
  const disposedAt = shellEvents().indexOf(`dispose ${from}`);
  const lastAttach = shellEvents().findLastIndex((event) => event.startsWith("attach"));
  expect(disposedAt).toBeGreaterThanOrEqual(0);
  expect(disposedAt).toBeLessThan(lastAttach);
  expect(sessions.live.has(from)).toBe(false);
  expect(sessions.live.has(Number(shellEvents()[lastAttach]!.split(" ")[1]))).toBe(true);
}

const inSplit = (conversationId: string) => {
  const split = readPaneSplit(conversationId);
  return split ? terminalLayoutKeys(split.layout).includes(SHELL) : false;
};

async function renderSession(
  conversationId: string,
  wrap: (node: ReactNode) => ReactNode,
  props: Omit<SessionProps, "conversationId"> = {},
  { shellListed = true }: { shellListed?: boolean } = {},
) {
  let current = props;
  const element = () => <>{wrap(<Session conversationId={conversationId} {...current} />)}</>;
  const view = render(element());
  // The views load TerminalView lazily: wait for the shell's first attach.
  if (shellListed) {
    await vi.waitFor(() => expect(liveFor(SHELL_ID)).toBe(1), { timeout: 5000 });
  } else {
    await vi.waitFor(() => expect(liveFor(AGENT_ID)).toBe(1), { timeout: 5000 });
  }
  await settle();
  return {
    /** Render the page again with ``next`` props, and the list as it is now. */
    update: async (next: Omit<SessionProps, "conversationId">) => {
      current = { ...current, ...next };
      await act(async () => view.rerender(element()));
      await settle();
    },
  };
}

beforeEach(() => {
  listed.terminals = TERMINALS;
  sessions.log.length = 0;
  sessions.live.clear();
  sessions.peak.clear();
  openRequests.length = 0;
  terminalMutations.create.mockClear();
  terminalMutations.remove.mockClear();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  resetTerminalPlacement();
  resetTerminalGroupsCache();
  resetTerminalOpenRequestsForTests();
});

describe.each([
  ["", (node: ReactNode) => node],
  [" under StrictMode", (node: ReactNode) => <StrictMode>{node}</StrictMode>],
])("a shell moving between the panes and the sidebar%s", (_mode, wrap) => {
  it("leaves the pane's session disposed before the sidebar attaches its own", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    await renderSession(conversationId, wrap);
    // Only the pane has the shell: the sidebar leaves a shell the split owns alone.
    expect(liveFor(SHELL_ID)).toBe(1);
    const paneSession = [...sessions.live.entries()].find(
      ([, entry]) => entry.terminalId === SHELL_ID,
    )![0];

    await act(async () => {
      expect(moveTerminalToSidebar(conversationId, SHELL, SCOPE)).toBeUndefined();
    });
    await settle();

    expect(inSplit(conversationId)).toBe(false);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
    expect(liveFor(SHELL_ID)).toBe(1);
    expectHandedOver(paneSession);
    // The pane it left isn't handed the keyboard back: the sidebar's shell is asked for it.
    expect(pendingTerminalFocus(conversationId, "main")?.tabKey).not.toBe(SHELL);
    expect(openRequests).toEqual([{ key: SHELL, focus: true }]);
    expect(terminalMutations.create).not.toHaveBeenCalled();
    expect(terminalMutations.remove).not.toHaveBeenCalled();
  });

  it("leaves the sidebar's session disposed before the pane attaches its own", async () => {
    const conversationId = nextConversation();
    await renderSession(conversationId, wrap);
    expect(liveFor(SHELL_ID)).toBe(1);
    const sidebarSession = [...sessions.live.entries()].find(
      ([, entry]) => entry.terminalId === SHELL_ID,
    )![0];

    await act(async () => {
      expect(openTerminalAsPane(conversationId, SHELL, SCOPE)).toBeUndefined();
    });
    await settle();

    expect(inSplit(conversationId)).toBe(true);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
    expect(liveFor(SHELL_ID)).toBe(1);
    expectHandedOver(sidebarSession);
    // The pane it joined has the split's focus and is asked for the keyboard.
    expect(readPaneSplit(conversationId)?.focusedKey).toBe(SHELL);
    expect(terminalMutations.create).not.toHaveBeenCalled();
    expect(terminalMutations.remove).not.toHaveBeenCalled();
  });

  it("gives up an attach still connecting as it moves: its late answer attaches nothing", async () => {
    const conversationId = nextConversation();
    await renderSession(conversationId, wrap);
    const connecting = [...sessions.live.entries()].find(
      ([, entry]) => entry.terminalId === SHELL_ID,
    )!;

    await act(async () => {
      openTerminalAsPane(conversationId, SHELL, SCOPE);
    });
    await settle();
    statuses.reported.length = 0;
    // The sidebar's socket answers only now, after it moved.
    await act(async () => {
      connecting[1].onState({ kind: "connected" } as unknown as ConnectionState);
    });
    await settle();

    expect(sessions.live.has(connecting[0])).toBe(false);
    expect(statuses.reported).toEqual([]);
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
  });

  it("is attached once when the Terminal view opens it on its own, beside its sidebar tab", async () => {
    const conversationId = nextConversation();
    // No split: the shell the sidebar opened takes over the Terminal view unsplit.
    await renderSession(conversationId, wrap, { initialTerminalKey: SHELL });
    // The Terminal view attaches it; its sidebar tab says where it is instead.
    expect(screen.getByText("This shell is open in the Terminal view.")).toBeInTheDocument();
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
    expect(terminalMutations.create).not.toHaveBeenCalled();
  });

  it("moves to its sidebar tab, attached once, as the Terminal view it opened in hides", async () => {
    const conversationId = nextConversation();
    const page = await renderSession(conversationId, wrap, { initialTerminalKey: SHELL });
    // Hidden, the Terminal view goes back to the agent's terminal and lets the shell go.
    await page.update({ visible: false });
    expect(screen.queryByText("This shell is open in the Terminal view.")).toBeNull();
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
  });

  it("stays attached once through hiding and showing the Terminal view again", async () => {
    const conversationId = nextConversation();
    const page = await renderSession(conversationId, wrap, { initialTerminalKey: SHELL });
    for (const visible of [false, true, false, true]) {
      // oxlint-disable-next-line no-await-in-loop -- each step settles before the next
      await page.update({ visible });
      expect(liveFor(SHELL_ID)).toBe(1);
    }
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
    expect(terminalMutations.create).not.toHaveBeenCalled();
  });

  it("is attached once when the list brings the shell only after the Terminal view opens", async () => {
    const conversationId = nextConversation();
    listed.terminals = [TERMINALS[0]!];
    const page = await renderSession(
      conversationId,
      wrap,
      { initialTerminalKey: SHELL },
      { shellListed: false },
    );
    expect(liveFor(SHELL_ID)).toBe(0);
    listed.terminals = TERMINALS;
    await page.update({});
    await vi.waitFor(() => expect(liveFor(SHELL_ID)).toBe(1), { timeout: 5000 });
    await settle();
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
  });

  it("shows a saved split's shell in the sidebar, attached once, for a viewer who can't split", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    listed.terminals = TERMINALS;
    await renderSession(conversationId, wrap, { readOnly: true });
    // The Terminal view shows no split, so the sidebar's tab shows the shell itself.
    expect(screen.queryByText("This shell is open in the Terminal view.")).toBeNull();
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
  });

  it("keeps a saved split's shell in the main view, attached once, while the agent's terminal isn't listed", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    listed.terminals = [TERMINALS[1]!];
    await renderSession(conversationId, wrap);
    // The split stays up through a gap in the list, so the sidebar's tab points to it.
    expect(screen.getByText("This shell is open in the Terminal view.")).toBeInTheDocument();
    expect(liveFor(SHELL_ID)).toBe(1);
    expect(sessions.peak.get(SHELL_ID)).toBe(1);
  });

  it("is attached once at every step of a quick out, back and out again", async () => {
    const conversationId = nextConversation();
    updatePaneSplit(conversationId, () => ({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, SHELL] },
      focusedKey: SHELL,
    }));
    await renderSession(conversationId, wrap);

    // All three in one go, before any attach or disposal has run.
    await act(async () => {
      moveTerminalToSidebar(conversationId, SHELL, SCOPE);
      openTerminalAsPane(conversationId, SHELL, SCOPE);
      moveTerminalToSidebar(conversationId, SHELL, SCOPE);
    });
    await settle();
    // And again with each step settled before the next.
    for (const move of [openTerminalAsPane, moveTerminalToSidebar, openTerminalAsPane]) {
      // oxlint-disable-next-line no-await-in-loop -- each move settles before the next
      await act(async () => {
        move(conversationId, SHELL, SCOPE);
      });
      // oxlint-disable-next-line no-await-in-loop -- as above
      await settle();
      expect(liveFor(SHELL_ID)).toBe(1);
    }

    expect(sessions.peak.get(SHELL_ID)).toBe(1);
    expect(inSplit(conversationId)).toBe(true);
    expect(terminalMutations.create).not.toHaveBeenCalled();
    expect(terminalMutations.remove).not.toHaveBeenCalled();
  });
});
