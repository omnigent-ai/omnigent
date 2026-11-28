// Every window-level binding's hook acts on each of its chords, on the Mac and elsewhere, so a
// registry entry can't advertise a chord nothing acts on. A binding's `module` says where its
// handler lives; each case here mounts that hook itself.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { PinIcon } from "lucide-react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { KeyboardShortcutsDialog } from "@/components/KeyboardShortcutsDialog";
import { useAddSiblingSessionHotkey } from "@/hooks/useAddSiblingSessionHotkey";
import type { Conversation } from "@/hooks/useConversations";
import { useApprovalPromptOnScreen, useApproveHotkey } from "@/hooks/useApproveHotkey";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useComposerConfigCycleHotkeys } from "@/hooks/useComposerConfigCycleHotkeys";
import { useFocusComposerHotkey } from "@/hooks/useFocusComposerHotkey";
import { resetHistoryNavigationForTests, useHistoryNavigation } from "@/hooks/useHistoryNavigation";
import { useMessageNavHotkeys } from "@/hooks/useMessageNavHotkeys";
import { useModelPickerHotkey } from "@/hooks/useModelPickerHotkey";
import { useNewBrowserHotkey } from "@/hooks/useNewBrowserHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";
import { useNewShellHotkey } from "@/hooks/useNewShellHotkey";
import { usePaneCycleHotkey } from "@/hooks/usePaneCycleHotkey";
import { useClaimRecentSessionChord, useRecentSessionHotkey } from "@/hooks/useRecentSessionSwitch";
import { useRenameSessionHotkey } from "@/hooks/useRenameSessionHotkey";
import { useSessionNumberHotkeys } from "@/hooks/useSessionNumberHotkeys";
import { useSettingsHotkey } from "@/hooks/useSettingsHotkey";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { useSidebarToggleHotkeys } from "@/hooks/useSidebarToggleHotkeys";
import { useTerminalSplitHotkey } from "@/hooks/useTerminalSplitHotkey";
import { useToggleUnreadHotkey } from "@/hooks/useToggleUnreadHotkey";
import { isExplicitlyUnread } from "@/hooks/useUnseenConversations";
import { useViewModeToggleHotkey } from "@/hooks/useViewModeToggleHotkey";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useWorkspaceTabHotkeys } from "@/hooks/useWorkspaceTabHotkeys";
import { registerFindSurface } from "@/lib/findRouting";
import type * as HotkeysModule from "@/lib/hotkeys";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { type Chord, chordKeyLabel } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  APP_SHORTCUT_BINDINGS,
  type ShortcutEnvironment,
  type ShortcutId,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";
import { useGoToFileCommands } from "@/shell/goToFile/goToFileCommands";
import { QuickPickShortcutHost } from "@/shell/quickPickShortcuts/QuickPickShortcutHost";
import { RecentSessionsSwitcher } from "@/shell/RecentSessionsSwitcher";
import { workspaceTabActionCommands } from "@/shell/workspaceCommands/model";
import { useWorkspaceFullScreenCommand } from "@/shell/workspaceCommands/useWorkspaceFullScreenCommand";
import { useWorkspaceHistoryCommands } from "@/shell/workspaceCommands/useWorkspaceHistoryCommands";
import { useWorkspaceTabChords } from "@/shell/workspaceCommands/useWorkspaceTabChords";
import { useFilesTreeToggleCommand } from "@/shell/workspaceTabs/placeViews/useFilesTreeToggleCommand";
import type { WorkspaceSidebarState } from "@/shell/workspaceTabs/WorkspaceSidebarLayout";
import { useWorkspaceTabStepChords } from "@/shell/workspaceTabs/useWorkspaceTabStepping";
import { AVAILABLE, usePaletteCommandsStore } from "@/store/paletteCommands";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/" }),
  useNavigationType: () => "PUSH",
  // The app's mount when embedded; identity standalone.
  useRebasePath: () => (path: string) => path,
}));

// A desktop-only binding switches the native shell on while its case runs; the platform is
// pinned per case, so each one presses the chord the registry shows on that platform.
const shell = vi.hoisted(() => ({ native: false, electron: false, mac: false }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isNativeShell: () => shell.native,
  isElectronShell: () => shell.electron,
  supportsDesktopCommands: () => false,
}));
vi.mock("@/lib/hotkeys", async (importOriginal) => ({
  ...(await importOriginal<typeof HotkeysModule>()),
  isMacPlatform: () => shell.mac,
}));
vi.mock("@/shell/commandContext", () => ({
  useCommandContext: () => ({ session: { id: "b" } }),
}));

const submitApproval = vi.fn();
vi.mock("@/store/chatStore", () => {
  const state = () => ({
    blocks: [{ type: "elicitation", elicitationId: "approval", status: "pending" }],
    submitApproval,
  });
  const useChatStore = (select: (current: ReturnType<typeof state>) => unknown) => select(state());
  return { useChatStore: Object.assign(useChatStore, { getState: state }) };
});

/** A keydown pressing `chord` on the given platform. */
function keydownFor(chord: Chord, mac: boolean): KeyboardEvent {
  return new KeyboardEvent("keydown", {
    key: chord.key ?? chordKeyLabel(chord).toLowerCase(),
    code: chord.code ?? "",
    metaKey: mac && !!chord.mod,
    ctrlKey: !!chord.ctrl || (!mac && !!chord.mod),
    altKey: !!chord.alt,
    shiftKey: !!chord.shift,
    bubbles: true,
    cancelable: true,
  });
}

interface HandlerCase {
  /**
   * Mounts the handler, once it's ready for the binding's chord at `chordIndex`, and returns a
   * check that it acted.
   */
  mount: (chordIndex: number) => (() => boolean) | Promise<() => boolean>;
  /** The shell the case runs in, where the binding has a chord only there; both platforms if unset. */
  environment?: Partial<ShortcutEnvironment> & { electron?: boolean };
}

function spyCase(
  mountWith: (spy: () => void) => void,
  environment?: HandlerCase["environment"],
): HandlerCase {
  return {
    environment,
    mount: () => {
      const spy = vi.fn();
      mountWith(spy);
      return () => spy.mock.calls.length > 0;
    },
  };
}

const navigatedTo = (target: string | number) => () =>
  navigate.mock.calls.some(([called]) => called === target);

function choices(onSelect: () => void) {
  return [
    { key: "a", label: "A", checked: true },
    { key: "b", label: "B", checked: false, onSelect },
  ];
}

function tabChordCase(action: "new-tab" | "close-tab" | "reopen"): HandlerCase {
  return {
    mount: () => {
      const run = vi.fn();
      const facts = { canReopen: true, selectedIndex: 0, tabCount: 2, sidebarOpen: true };
      const commands = workspaceTabActionCommands(facts);
      renderHook(() => useWorkspaceTabChords({ conversationId: "c", commands, facts, run }));
      return () => run.mock.calls.some(([called]) => called.action === action);
    },
  };
}

function historyCase(direction: "back" | "forward"): HandlerCase {
  return {
    mount: () => {
      const go = vi.fn();
      const somewhere = { target: "a.ts", waitingForPage: false };
      renderHook(() =>
        useWorkspaceHistoryCommands({
          conversationId: "c",
          facts: { back: somewhere, forward: somewhere },
          go,
        }),
      );
      return () => go.mock.calls.some(([called]) => called === direction);
    },
  };
}

function tabStepCase(direction: -1 | 1): HandlerCase {
  return {
    environment: { native: true },
    mount: () => {
      const step = vi.fn();
      renderHook(() => useWorkspaceTabStepChords(step));
      return () => step.mock.calls.some(([called]) => called === direction);
    },
  };
}

/** The ⌘K palette's QuickPick shortcut host, which runs each chord's ⌘K command. */
function quickPickHostCase(
  check: () => boolean,
  environment?: HandlerCase["environment"],
): HandlerCase {
  return {
    environment,
    mount: () => {
      useSidebarSessionOrderStore.setState({
        orderedSessionIds: ["a", "b", "c"],
        activeSessionId: "b",
        pendingSessionId: null,
      });
      render(
        <QueryClientProvider client={new QueryClient()}>
          <QuickPickShortcutHost />
        </QueryClientProvider>,
      );
      return check;
    },
  };
}

/** The open session's header command `command`, which its chord runs through the host. */
function openSessionCase(command: "pin" | "archive" | "fork", mac: boolean): HandlerCase {
  const run = vi.fn();
  const hosted = quickPickHostCase(() => run.mock.calls.length > 0, { mac });
  return {
    environment: hosted.environment,
    mount: (chordIndex) => {
      run.mockReset();
      usePaletteCommandsStore.setState({
        groups: {
          "open-session:b": {
            heading: "Session",
            commands: [
              {
                id: `open-session:b:${command}`,
                label: command,
                icon: PinIcon,
                keywords: [],
                availability: AVAILABLE,
                run,
              },
            ],
          },
        },
      });
      return hosted.mount(chordIndex);
    },
  };
}

function recentSwitchCase(direction: 1 | -1): HandlerCase {
  return {
    // A browser keeps Ctrl+[ / Ctrl+] off the Mac, so the switch binds them there only.
    environment: { mac: true },
    mount: () => {
      const onSwitch = vi.fn();
      renderHook(() => {
        useRecentSessionHotkey(true);
        useClaimRecentSessionChord(true, onSwitch);
      });
      return () => onSwitch.mock.calls.some(([called]) => called === direction);
    },
  };
}

function messageStepCase(direction: "goPrev" | "goNext"): HandlerCase {
  return spyCase((spy) =>
    renderHook(() =>
      useMessageNavHotkeys(
        {
          goPrev: direction === "goPrev" ? spy : () => {},
          goNext: direction === "goNext" ? spy : () => {},
        },
        true,
      ),
    ),
  );
}

function goToFileCase(mode: "goToFile" | "searchText"): HandlerCase {
  return spyCase((spy) =>
    renderHook(() =>
      useGoToFileCommands("c", {
        textSearch: true,
        goToFile: mode === "goToFile" ? spy : () => {},
        searchText: mode === "searchText" ? spy : () => {},
      }),
    ),
  );
}

const SESSION_SLOTS = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"];

const RECENT_CONVERSATIONS = [
  { id: "a", title: "A", updated_at: 2 },
  { id: "b", title: "B", updated_at: 1 },
] as unknown as Conversation[];

let unreadSessions = 0;

const HANDLERS: Partial<Record<ShortcutId, HandlerCase>> = {
  "palette.commands": spyCase((spy) => renderHook(() => useCommandPaletteHotkey(spy))),
  "palette.sessions": spyCase((spy) =>
    renderHook(() => useCommandPaletteHotkey(() => {}, true, undefined, spy)),
  ),
  "shortcuts.show": {
    mount: () => {
      render(<KeyboardShortcutsDialog />);
      return () => screen.queryByRole("dialog") !== null;
    },
  },
  "session.new": {
    mount: () => {
      renderHook(() => useNewSessionHotkey());
      return navigatedTo("/");
    },
  },
  "session.addSibling": spyCase((spy) => renderHook(() => useAddSiblingSessionHotkey(spy, true))),
  "session.rename": spyCase((spy) => renderHook(() => useRenameSessionHotkey(spy))),
  // A session of its own per press, since the unread mark outlives the case.
  "session.toggleUnread": {
    mount: () => {
      const id = `conv-${(unreadSessions += 1)}`;
      renderHook(() => useToggleUnreadHotkey({ id, updated_at: 5_000 }));
      return () => isExplicitlyUnread(id);
    },
  },
  "settings.open": {
    mount: () => {
      renderHook(() => useSettingsHotkey());
      return navigatedTo("/settings");
    },
  },
  // The page takes ⌘[ / ⌘] only in the desktop app, where nothing else steps history.
  "history.back": {
    environment: { electron: true },
    mount: () => {
      window.history.replaceState({ idx: 1 }, "");
      renderHook(() => useHistoryNavigation());
      return navigatedTo(-1);
    },
  },
  "history.forward": {
    environment: { electron: true },
    mount: () => {
      renderHook(() => useHistoryNavigation());
      return navigatedTo(1);
    },
  },
  "sessions.recent": {
    mount: () => {
      render(
        <RecentSessionsSwitcher conversations={RECENT_CONVERSATIONS} activeSessionId="b" enabled />,
      );
      return () => screen.queryByText("A") !== null;
    },
  },
  // Each digit opens the session in its slot.
  "session.number": {
    mount: (chordIndex) => {
      renderHook(() => useSessionNumberHotkeys(SESSION_SLOTS));
      return navigatedTo(`/c/${SESSION_SLOTS[chordIndex]}`);
    },
  },
  "view.mode": {
    mount: () => {
      const setView = vi.fn();
      renderHook(() => useViewModeToggleHotkey({ enabled: true, view: "chat", setView }));
      return () => setView.mock.calls.length > 0;
    },
  },
  "sidebar.left": spyCase((spy) =>
    renderHook(() => useSidebarToggleHotkeys({ onToggleLeft: spy })),
  ),
  "sidebar.right": spyCase((spy) =>
    renderHook(() => useSidebarToggleHotkeys({ onToggleRight: spy })),
  ),
  "workspace.tab": spyCase((spy) => {
    const select = {
      current: () => {
        spy();
        return true;
      },
    };
    renderHook(() => useWorkspaceTabHotkeys(select));
  }),
  "terminal.new": spyCase((spy) => renderHook(() => useNewShellHotkey(spy))),
  "model.picker": spyCase((spy) => renderHook(() => useModelPickerHotkey(spy))),
  "model.cycle": spyCase((spy) =>
    renderHook(() => useComposerConfigCycleHotkeys(choices(spy), undefined)),
  ),
  "effort.cycle": spyCase((spy) =>
    renderHook(() => useComposerConfigCycleHotkeys(undefined, choices(spy))),
  ),
  "composer.focus": spyCase((spy) => renderHook(() => useFocusComposerHotkey(spy))),
  "dictation.toggle": spyCase((spy) => renderHook(() => useVoiceDictationHotkey(spy))),
  "approval.accept": {
    mount: () => {
      renderHook(() => useApproveHotkey());
      return () => submitApproval.mock.calls.length > 0;
    },
  },
  // Esc declines only a prompt whose card is on screen.
  "approval.decline": {
    mount: () => {
      renderHook(() => {
        useApproveHotkey();
        useApprovalPromptOnScreen("approval", true);
      });
      return () => submitApproval.mock.calls.some(([, verdict]) => verdict === "decline");
    },
  },
  "pane.previous": {
    mount: () => {
      const onFocusPane = vi.fn();
      renderHook(() =>
        usePaneCycleHotkey({ enabled: true, paneKeys: ["a", "b"], focusedKey: "b", onFocusPane }),
      );
      return () => onFocusPane.mock.calls.some(([key]) => key === "a");
    },
  },
  "pane.next": {
    mount: () => {
      const onFocusPane = vi.fn();
      renderHook(() =>
        usePaneCycleHotkey({ enabled: true, paneKeys: ["a", "b"], focusedKey: "a", onFocusPane }),
      );
      return () => onFocusPane.mock.calls.some(([key]) => key === "b");
    },
  },
  "terminal.splitRight": spyCase((spy) => renderHook(() => useTerminalSplitHotkey("columns", spy))),
  "terminal.splitDown": spyCase((spy) => renderHook(() => useTerminalSplitHotkey("rows", spy))),
  "workspace.newTab": tabChordCase("new-tab"),
  "workspace.closeTab": tabChordCase("close-tab"),
  "workspace.reopenTab": tabChordCase("reopen"),
  "workspace.back": historyCase("back"),
  "workspace.forward": historyCase("forward"),
  // A browser keeps ⇧⌘[ / ⇧⌘] for its own tabs.
  "workspace.previousTab": tabStepCase(-1),
  "workspace.nextTab": tabStepCase(1),
  "browser.new": spyCase((spy) => renderHook(() => useNewBrowserHotkey(spy))),
  "files.toggleTree": spyCase((spy) =>
    renderHook(() =>
      useFilesTreeToggleCommand(
        { shown: true, toggle: spy } as unknown as WorkspaceSidebarState,
        true,
      ),
    ),
  ),
  "session.next": quickPickHostCase(navigatedTo("/c/c")),
  "session.previous": quickPickHostCase(navigatedTo("/c/a")),
  "session.pin": openSessionCase("pin", true),
  "session.archive": openSessionCase("archive", false),
  "session.fork": openSessionCase("fork", true),
  "session.recent": recentSwitchCase(1),
  "session.recentReverse": recentSwitchCase(-1),
  "message.previous": messageStepCase("goPrev"),
  "message.next": messageStepCase("goNext"),
  "palette.files": goToFileCase("goToFile"),
  "palette.text": goToFileCase("searchText"),
  // Its chord binds once the tabs sidebar's model has loaded, which ⌘K's row shows.
  "workspace.toggleFullScreen": {
    mount: async () => {
      const toggle = vi.fn();
      renderHook(() =>
        useWorkspaceFullScreenCommand({ conversationId: "c", fullScreen: false, toggle }),
      );
      await vi.waitFor(() =>
        expect(usePaletteCommandsStore.getState().groups["workspace-full-screen:c"]).toBeDefined(),
      );
      return () => toggle.mock.calls.length > 0;
    },
  },
  "find.open": {
    mount: () => {
      const openFind = vi.fn();
      onTestFinished(registerFindSurface({ isActive: () => true, openFind }));
      return () => openFind.mock.calls.length > 0;
    },
  },
};

// Flagged bindings too: the registry doesn't check a binding's flag, so their chords match.
const ALL_BINDINGS = [...APP_SHORTCUT_BINDINGS, ...WORKSPACE_SIDEBAR_BINDINGS];
const appBindings = ALL_BINDINGS.filter((binding) => binding.handler.scope === "app");

type CaseEnvironment = Partial<ShortcutEnvironment> & { mac: boolean };

/** The shells `id`'s case runs in: its pinned platform, or the Mac and elsewhere. */
function environmentsOf(id: ShortcutId): CaseEnvironment[] {
  const { electron = false, ...environment } = HANDLERS[id]?.environment ?? {};
  const platforms = environment.mac === undefined ? [false, true] : [environment.mac];
  return platforms.map((mac) => ({ native: electron, ...environment, mac }));
}

interface ChordPress {
  id: ShortcutId;
  environment: CaseEnvironment;
  chordIndex: number;
  chord: Chord;
}

/** Every chord each window-level binding has, in every shell its case runs in. */
const CHORD_PRESSES: ChordPress[] = appBindings.flatMap(({ id }) =>
  environmentsOf(id).flatMap((environment) =>
    activeShortcutChords(id, environment).map((chord, chordIndex) => ({
      id,
      environment,
      chordIndex,
      chord,
    })),
  ),
);

/** Mount the binding's handler in its case's shell and press the chord there. */
async function pressFor({ id, environment, chordIndex, chord }: ChordPress): Promise<boolean> {
  const handlerCase = HANDLERS[id]!;
  shell.mac = environment.mac;
  shell.native = environment.native ?? false;
  shell.electron = handlerCase.environment?.electron ?? false;
  const acted = await handlerCase.mount(chordIndex);
  act(() => {
    document.body.dispatchEvent(keydownFor(chord, environment.mac));
  });
  return acted();
}

const INITIAL_SESSION_ORDER = useSidebarSessionOrderStore.getState();

afterEach(() => {
  cleanup();
  shell.native = false;
  shell.electron = false;
  shell.mac = false;
  localStorage.clear();
  navigate.mockReset();
  submitApproval.mockReset();
  usePaletteCommandsStore.setState({ groups: {} });
  useSidebarSessionOrderStore.setState(INITIAL_SESSION_ORDER, true);
  window.history.replaceState(null, "");
  resetHistoryNavigationForTests();
});

describe("shortcut registry handlers", () => {
  it("names an existing handler module for every binding matched through the registry", () => {
    const modules = new Set(
      Object.keys(import.meta.glob("/src/**/*.{ts,tsx}")).map((path) => path.slice(5)),
    );
    for (const { id, handler } of ALL_BINDINGS) {
      if (handler.scope === "unhandled" || handler.scope === "elsewhere") continue;
      expect(modules.has(handler.module), `${id}: ${handler.module}`).toBe(true);
    }
  });

  it("leaves only the listed bindings without a handler", () => {
    const unhandled = ALL_BINDINGS.filter(({ handler }) => handler.scope === "unhandled");
    // The pull request page's keys; nothing mounted binds them yet.
    expect(unhandled.map(({ id }) => id)).toEqual([
      "pullRequests.removeSelected",
      "pullRequestStack.next",
      "pullRequestStack.previous",
    ]);
  });

  it("has a handler case for every window-level binding, and no stale ones", () => {
    expect(Object.keys(HANDLERS).sort()).toEqual(appBindings.map(({ id }) => id).sort());
  });

  it("presses a chord for every window-level binding", () => {
    const pressed = new Set(CHORD_PRESSES.map(({ id }) => id));
    expect(appBindings.map(({ id }) => id).filter((id) => !pressed.has(id))).toEqual([]);
  });

  it.each(
    CHORD_PRESSES.map((press) => ({
      ...press,
      name: `${press.id}, chord ${press.chordIndex + 1} ${press.environment.mac ? "on the Mac" : "elsewhere"}`,
    })),
  )("runs $name", async (press) => {
    expect(await pressFor(press)).toBe(true);
  });
});
