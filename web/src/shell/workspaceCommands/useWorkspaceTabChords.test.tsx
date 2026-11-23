import { fireEvent, renderHook } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import { activeShortcutChords } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import { type WorkspaceTabListFacts, workspaceTabActionCommands } from "./model";
import { useWorkspaceTabChords } from "./useWorkspaceTabChords";
import { LAST_NEW_TAB_REASON } from "../workspaceTabs/workspaceTabsStore";

vi.mock("sonner", () => ({ toast: { info: vi.fn() } }));

const OPEN_TABS: WorkspaceTabListFacts = {
  canReopen: true,
  selectedIndex: 0,
  tabCount: 2,
  sidebarOpen: true,
};

function mount(facts: WorkspaceTabListFacts = OPEN_TABS) {
  const run = vi.fn();
  const view = renderHook(
    ({ current }: { current: WorkspaceTabListFacts }) =>
      useWorkspaceTabChords(
        {
          conversationId: "c",
          commands: workspaceTabActionCommands(current),
          facts: current,
          run,
        },
        true,
      ),
    { initialProps: { current: facts } },
  );
  return { run, rerender: (next: WorkspaceTabListFacts) => view.rerender({ current: next }), view };
}

const actions = (run: ReturnType<typeof vi.fn>) =>
  run.mock.calls.map(([command]) => (command as { action: string }).action);

const press = (code: string, modifiers: { alt?: boolean; shift?: boolean } = {}) =>
  fireEvent.keyDown(window, {
    code,
    key: code.slice(3).toLowerCase(),
    metaKey: true,
    altKey: modifiers.alt ?? false,
    shiftKey: modifiers.shift ?? false,
  });

afterEach(() => {
  vi.mocked(toast.info).mockClear();
});

describe("useWorkspaceTabChords", () => {
  it("runs New, Close and Reopen from ⌘⌥⇧N, ⌘⌥W and ⌘⌥⇧T where ⌘W isn't the page's", () => {
    const { run } = mount();
    press("KeyN", { alt: true, shift: true });
    press("KeyW", { alt: true });
    press("KeyT", { alt: true, shift: true });
    expect(actions(run)).toEqual(["new-tab", "close-tab", "reopen"]);
  });

  it("with the sidebar closed, still opens a new tab and reopens one, but leaves Close alone", () => {
    const { run } = mount({ ...OPEN_TABS, sidebarOpen: false });
    press("KeyW", { alt: true });
    press("KeyT", { alt: true, shift: true });
    press("KeyN", { alt: true, shift: true });
    expect(actions(run)).toEqual(["reopen", "new-tab"]);
  });

  it("says why ⌘⌥W can't close the only empty New tab, and closes nothing", () => {
    const { run } = mount({ ...OPEN_TABS, tabCount: 1, selectedCloseRefusal: LAST_NEW_TAB_REASON });
    press("KeyW", { alt: true });
    expect(run).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalledWith(LAST_NEW_TAB_REASON);
    // An unavailable Move left, by contrast, stays quiet.
    vi.mocked(toast.info).mockClear();
    press("ArrowLeft", { alt: true, shift: true });
    expect(toast.info).not.toHaveBeenCalled();
  });

  it("lists each as a ⌘K row with its chord's key caps", () => {
    usePaletteCommandsStore.setState(usePaletteCommandsStore.getInitialState());
    mount();
    expect(
      registeredPaletteCommands()
        .filter((command) => command.id.startsWith("workspace:c:tab-action"))
        .map((command) => [command.label, command.nativeShortcut]),
    ).toEqual([
      ["New workspace tab", activeShortcutChords("workspace.newTab", { mac: true })[0]],
      ["Close workspace tab", activeShortcutChords("workspace.closeTab", { mac: true })[0]],
      [
        "Reopen closed workspace tab",
        activeShortcutChords("workspace.reopenTab", { mac: true })[0],
      ],
    ]);
  });

  it("leaves plain ⌘W and ⌘T to the browser and the app", () => {
    const { run } = mount();
    press("KeyW");
    press("KeyT");
    expect(run).not.toHaveBeenCalled();
  });
});
