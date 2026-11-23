// Every window-level binding fires the handler the registry names when its chords are
// pressed, so a registry entry can't advertise a chord nothing acts on.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { PinIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyboardShortcutsDialog } from "@/components/KeyboardShortcutsDialog";
import type { Conversation } from "@/hooks/useConversations";
import { useAddSiblingSessionHotkey } from "@/hooks/useAddSiblingSessionHotkey";
import { useApprovalPromptOnScreen, useApproveHotkey } from "@/hooks/useApproveHotkey";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useComposerConfigCycleHotkeys } from "@/hooks/useComposerConfigCycleHotkeys";
import { useFocusComposerHotkey } from "@/hooks/useFocusComposerHotkey";
import { useHistoryNavigation } from "@/hooks/useHistoryNavigation";
import { useMessageNavHotkeys } from "@/hooks/useMessageNavHotkeys";
import { useModelPickerHotkey } from "@/hooks/useModelPickerHotkey";
import { useNewBrowserHotkey } from "@/hooks/useNewBrowserHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";
import { useNewShellHotkey } from "@/hooks/useNewShellHotkey";
import { usePaneCycleHotkey } from "@/hooks/usePaneCycleHotkey";
import {
  useRecentSessionHotkey,
  useRecentSessionSwitchStore,
} from "@/hooks/useRecentSessionSwitch";
import { useRenameSessionHotkey } from "@/hooks/useRenameSessionHotkey";
import { useSessionNumberHotkeys } from "@/hooks/useSessionNumberHotkeys";
import { useSidebarSessionOrderStore } from "@/hooks/useSidebarSessionOrder";
import { useSettingsHotkey } from "@/hooks/useSettingsHotkey";
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
  chordAriaShortcut,
  primaryShortcutChords,
  QUICK_PICK_SHORTCUT_BINDINGS,
  SHORTCUT_BINDINGS,
  type ShortcutEnvironment,
  type ShortcutId,
  WORKSPACE_SIDEBAR_BINDINGS,
} from "@/lib/shortcuts/registry";
import { QuickPickShortcutHost } from "@/shell/quickPickShortcuts/QuickPickShortcutHost";
import { RecentSessionsSwitcher } from "@/shell/RecentSessionsSwitcher";
import { workspaceTabActionCommands } from "@/shell/workspaceCommands/model";
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
  /** Mounts the handler and returns a check that it acted. */
  mount: () => () => boolean;
  /** The shell the case runs in, where the binding has a chord only there. */
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

/** The shortcut host inside a query client, as in the app shell. */
const renderShortcutHost = () =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuickPickShortcutHost />
    </QueryClientProvider>,
  );

/** Register the open session's header row `command`, which the shortcut host runs. */
function headerRowCase(command: string): HandlerCase {
  return {
    mount: () => {
      const run = vi.fn();
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
      renderShortcutHost();
      return () => run.mock.calls.length > 0;
    },
  };
}

const RECENT_CONVERSATIONS = [
  { id: "a", title: "A", updated_at: 2 },
  { id: "b", title: "B", updated_at: 1 },
] as unknown as Conversation[];

const HANDLERS: Partial<Record<ShortcutId, HandlerCase>> = {
  "palette.commands": spyCase((spy) => renderHook(() => useCommandPaletteHotkey(spy))),
  "palette.sessions": spyCase((spy) =>
    renderHook(() => useCommandPaletteHotkey(() => {}, true, false, spy)),
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
  "session.toggleUnread": {
    mount: () => {
      renderHook(() => useToggleUnreadHotkey({ id: "conv-1", updated_at: 5_000 }));
      return () => isExplicitlyUnread("conv-1");
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
  "session.number": {
    mount: () => {
      renderHook(() => useSessionNumberHotkeys(["a", "b"], "b"));
      return navigatedTo("/c/a");
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
  "session.next": {
    mount: () => {
      renderShortcutHost();
      return navigatedTo("/c/c");
    },
  },
  "session.previous": {
    mount: () => {
      renderShortcutHost();
      return navigatedTo("/c/a");
    },
  },
  // Recency has a chord on macOS only.
  "session.recent": {
    environment: { mac: true },
    mount: () => {
      renderHook(() => useRecentSessionHotkey(true));
      return () => useRecentSessionSwitchStore.getState().switcher !== null;
    },
  },
  "session.recentReverse": {
    environment: { mac: true },
    mount: () => {
      renderHook(() => useRecentSessionHotkey(true));
      return () => useRecentSessionSwitchStore.getState().switcher !== null;
    },
  },
  // In a browser tab on macOS, where the desktop menu doesn't own them.
  "session.pin": { ...headerRowCase("pin"), environment: { mac: true } },
  "session.archive": { ...headerRowCase("archive"), environment: { mac: true } },
  "session.fork": { ...headerRowCase("fork"), environment: { mac: true } },
  "message.previous": spyCase((spy) =>
    renderHook(() => useMessageNavHotkeys({ goPrev: spy, goNext: () => {} })),
  ),
  "message.next": spyCase((spy) =>
    renderHook(() => useMessageNavHotkeys({ goPrev: () => {}, goNext: spy })),
  ),
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
  "find.open": {
    mount: () => {
      const openFind = vi.fn();
      const release = registerFindSurface({ isActive: () => true, openFind });
      return () => {
        release();
        return openFind.mock.calls.length > 0;
      };
    },
  },
};

const ALL_BINDINGS = [
  ...SHORTCUT_BINDINGS,
  ...QUICK_PICK_SHORTCUT_BINDINGS,
  ...WORKSPACE_SIDEBAR_BINDINGS,
];
const appBindings = ALL_BINDINGS.filter((binding) => binding.handler.scope === "app");

function environmentOf(id: ShortcutId): Partial<ShortcutEnvironment> & { mac: boolean } {
  const { electron = false, ...environment } = HANDLERS[id]?.environment ?? {};
  return { mac: false, native: electron, ...environment };
}

/** Mount `id`'s handler in its case's shell and press `chord` there. */
function pressFor(id: ShortcutId, chord: Chord): boolean {
  const handlerCase = HANDLERS[id]!;
  const environment = environmentOf(id);
  shell.mac = environment.mac;
  shell.native = environment.native ?? false;
  shell.electron = handlerCase.environment?.electron ?? false;
  const acted = handlerCase.mount();
  act(() => {
    document.body.dispatchEvent(keydownFor(chord, environment.mac));
  });
  return acted();
}

beforeEach(() => {
  useSidebarSessionOrderStore.setState({
    orderedSessionIds: ["a", "b", "c"],
    activeSessionId: "b",
    pendingSessionId: null,
  });
  const live = new Set(["a", "b"]);
  useRecentSessionSwitchStore.setState({
    history: ["b", "a"],
    readLiveSessionIds: () => live,
    sessionsKnown: true,
    switcher: null,
    chordOwner: null,
  });
});

afterEach(() => {
  cleanup();
  shell.native = false;
  shell.electron = false;
  shell.mac = false;
  localStorage.clear();
  navigate.mockReset();
  submitApproval.mockReset();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("shortcut registry handlers", () => {
  it("names a handler module for every binding that exists", () => {
    const modules = new Set(
      Object.keys(import.meta.glob("/src/**/*.{ts,tsx}")).map((path) => path.slice(5)),
    );
    for (const { id, handler } of ALL_BINDINGS) {
      if (handler.scope === "unhandled" || handler.scope === "menu") continue;
      expect(modules.has(handler.module), `${id}: ${handler.module}`).toBe(true);
    }
  });

  it("leaves only the listed bindings without a handler", () => {
    const unhandled = ALL_BINDINGS.filter(({ handler }) => handler.scope === "unhandled");
    expect(unhandled.map(({ id }) => id)).toEqual([
      "workspace.searchContents",
      "workspace.toggleFullScreen",
      "palette.text",
      "palette.files",
    ]);
  });

  it("has a handler case for every window-level binding, and no stale ones", () => {
    expect(Object.keys(HANDLERS).sort()).toEqual(appBindings.map(({ id }) => id).sort());
  });

  it.each(appBindings.map(({ id }) => id))("runs %s from the chord it shows", (id) => {
    const chord = primaryShortcutChords(id, environmentOf(id))[0];
    expect(chord, `${id} has no chord here`).toBeDefined();
    expect(pressFor(id, chord!)).toBe(true);
  });

  const secondaryCases = appBindings.flatMap(({ id }) => {
    const environment = environmentOf(id);
    const shown = primaryShortcutChords(id, environment).length;
    return activeShortcutChords(id, environment)
      .slice(shown)
      .map((chord) => [id, chordAriaShortcut(chord, environment.mac), chord] as const);
  });

  it("has window-level bindings with secondary chords to run", () => {
    expect(secondaryCases.map(([id]) => id)).toEqual(["message.previous", "message.next"]);
  });

  it.each(secondaryCases)("runs %s from its secondary chord %s", (id, _aria, chord) => {
    expect(pressFor(id, chord)).toBe(true);
  });
});
