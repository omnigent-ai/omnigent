import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider } from "./ActionProvider";
import { KeybindingDispatcher } from "./KeybindingDispatcher";
import {
  getKeybindingSnapshot,
  replaceAllUserKeybindings,
  resetAllUserKeybindings,
  resetKeybindingStoreForTesting,
  resetUserKeybindingRule,
  setUserKeybindingRule,
  subscribeKeybindings,
  unbindDefaultKeybinding,
  useKeybindingSnapshot,
} from "./KeybindingStore";
import { KEYBINDINGS_STORAGE_KEY } from "./keybindingPreferences";
import { HANDLED } from "./types";
import { useRegisterAction } from "./useRegisterAction";

function Probe() {
  const snapshot = useKeybindingSnapshot();
  return (
    <output data-testid="snapshot">
      {snapshot.userRules.map((rule) => `${rule.id}:${rule.sequence}`).join(",")}
    </output>
  );
}

function CommandHandler({ run }: { run: () => typeof HANDLED }) {
  useRegisterAction("workbench.action.showCommands", { run, acceptsKeybindings: true });
  return null;
}

beforeEach(() => {
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  resetKeybindingStoreForTesting();
  vi.restoreAllMocks();
});

describe("KeybindingStore", () => {
  it("returns stable immutable snapshots while storage is unchanged", () => {
    const first = getKeybindingSnapshot();
    const second = getKeybindingSnapshot();
    expect(second).toBe(first);
    expect(Object.isFrozen(first)).toBe(true);
    expect(Object.isFrozen(first.defaultRules)).toBe(true);
    expect(Object.isFrozen(first.effectiveRules)).toBe(true);
    expect(Object.isFrozen(first.effectiveRules[0]!.sequence)).toBe(true);
  });

  it("refreshes imperative reads before any subscriber installs storage sync", () => {
    expect(getKeybindingSnapshot().userRules).toEqual([]);
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        {
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        },
      ]),
    );
    expect(getKeybindingSnapshot().userRules[0]?.id).toBe("workbench.showCommands");
  });

  it("keeps imperative snapshots pure and the first subscription identity-stable", () => {
    const add = vi.spyOn(window, "addEventListener");
    const first = getKeybindingSnapshot();
    expect(add.mock.calls.filter(([type]) => type === "storage")).toHaveLength(0);
    let renders = 0;
    function CountingProbe() {
      renders += 1;
      useKeybindingSnapshot();
      return null;
    }
    render(<CountingProbe />);
    expect(renders).toBe(1);
    expect(getKeybindingSnapshot()).toBe(first);
  });

  it("reacts to same-tab add, replace, unbind, and reset mutations", async () => {
    render(<Probe />);
    expect(screen.getByTestId("snapshot")).toHaveTextContent("");

    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+shift+k",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    await waitFor(() =>
      expect(screen.getByTestId("snapshot")).toHaveTextContent(
        "workbench.showCommands:ctrl+shift+k",
      ),
    );
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+shift+k",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: false });

    expect(
      unbindDefaultKeybinding({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    await waitFor(() =>
      expect(screen.getByTestId("snapshot")).toHaveTextContent("workbench.showCommands:null"),
    );
    expect(resetUserKeybindingRule("workbench.showCommands")).toEqual({
      ok: true,
      changed: true,
    });
    await waitFor(() => expect(screen.getByTestId("snapshot")).toHaveTextContent(""));
  });

  it("rejects invalid mutations and failed writes without losing stored overrides", () => {
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+shift+k",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    const before = localStorage.getItem(KEYBINDINGS_STORAGE_KEY);
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+",
        mode: "global",
      } as never),
    ).toEqual({ ok: false, reason: "invalidRule" });
    expect(localStorage.getItem(KEYBINDINGS_STORAGE_KEY)).toBe(before);

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(
      setUserKeybindingRule({
        id: "user.palette.alternate",
        action: "workbench.action.showCommands",
        sequence: "ctrl+alt+k",
        mode: "global",
      }),
    ).toEqual({ ok: false, reason: "storageUnavailable" });
    expect(getKeybindingSnapshot().userRules).toHaveLength(1);
  });

  it("rejects semantically unusable known mutation inputs", () => {
    expect(
      setUserKeybindingRule({
        id: "session.new",
        action: "session.action.new",
        sequence: "ctrl+n",
        mode: "composer",
      }),
    ).toEqual({ ok: false, reason: "unusableRule" });
    expect(
      setUserKeybindingRule({
        id: "session.openPinned.native.1",
        action: "session.action.openPinned",
        sequence: "ctrl+1",
        mode: "global",
        args: { slot: 99 },
      }),
    ).toEqual({ ok: false, reason: "unusableRule" });
    expect(
      setUserKeybindingRule({
        id: "user.terminal.alternate",
        action: "terminal.action.sendSequence",
        sequence: "ctrl+enter",
        mode: "terminal",
      } as never),
    ).toEqual({ ok: false, reason: "unusableRule" });
    expect(localStorage.getItem(KEYBINDINGS_STORAGE_KEY)).toBeNull();
  });

  it("re-reads storage when subscribers remount after an inactive interval", async () => {
    const first = render(<Probe />);
    first.unmount();
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        {
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        },
      ]),
    );
    render(<Probe />);
    await waitFor(() =>
      expect(screen.getByTestId("snapshot")).toHaveTextContent(
        "workbench.showCommands:ctrl+shift+k",
      ),
    );
  });

  it("preserves dormant unknown actions through a known same-tab mutation", () => {
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        { id: "future", action: "future.action.run", sequence: "mod+j", mode: "global" },
      ]),
    );
    resetKeybindingStoreForTesting();
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+shift+k",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    expect(getKeybindingSnapshot().userRules.map((rule) => rule.id)).toEqual([
      "future",
      "workbench.showCommands",
    ]);
  });

  it("diagnoses the cap when dormant rows fill storage", () => {
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify(
        Array.from({ length: 500 }, (_, index) => ({
          id: `future-${index}`,
          action: `future.action.${index}`,
          sequence: "mod+j",
          mode: "global",
        })),
      ),
    );
    resetKeybindingStoreForTesting();
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "ctrl+shift+k",
        mode: "global",
      }),
    ).toEqual({ ok: false, reason: "limitReached" });
  });

  it("supports validated bulk replacement for import flows", () => {
    expect(
      replaceAllUserKeybindings([
        { id: "future", action: "future.action.run", sequence: "mod+j", mode: "global" },
      ]),
    ).toEqual({ ok: true, changed: true });
    expect(getKeybindingSnapshot().userRules[0]?.action).toBe("future.action.run");
    expect(replaceAllUserKeybindings(getKeybindingSnapshot().userRules)).toEqual({
      ok: true,
      changed: false,
    });
    expect(
      replaceAllUserKeybindings([
        { id: "broken", action: "future.action.run", sequence: "ctrl+", mode: "global" },
      ]),
    ).toEqual({ ok: false, reason: "invalidRule" });
  });

  it("updates subscribers after a cross-tab storage event", async () => {
    render(<Probe />);
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        {
          id: "future",
          action: "future.action.run",
          sequence: "mod+j",
          mode: "global",
        },
      ]),
    );
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: KEYBINDINGS_STORAGE_KEY,
        newValue: localStorage.getItem(KEYBINDINGS_STORAGE_KEY),
      }),
    );
    await waitFor(() => expect(screen.getByTestId("snapshot")).toHaveTextContent("future:mod+j"));
    expect(getKeybindingSnapshot().effectiveRules.some((rule) => rule.id === "future")).toBe(false);
  });

  it("shares one persistent storage listener across subscribers", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const unsubscribers = [
      subscribeKeybindings(vi.fn()),
      subscribeKeybindings(vi.fn()),
      subscribeKeybindings(vi.fn()),
    ];
    expect(add.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
    unsubscribers.forEach((unsubscribe) => unsubscribe());
    expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(0);
    resetKeybindingStoreForTesting();
    expect(remove.mock.calls.filter(([type]) => type === "storage")).toHaveLength(1);
  });

  it("notifies direct subscriptions for matching keys and storage.clear", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeKeybindings(listener);
    window.dispatchEvent(new StorageEvent("storage", { key: "unrelated" }));
    expect(listener).not.toHaveBeenCalled();
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: KEYBINDINGS_STORAGE_KEY,
        newValue: JSON.stringify([
          { id: "future", action: "future.action.run", sequence: "mod+j", mode: "global" },
        ]),
      }),
    );
    window.dispatchEvent(new StorageEvent("storage", { key: null }));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it("drives production dispatch from a manually seeded valid override", () => {
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        {
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        },
      ]),
    );
    resetKeybindingStoreForTesting();
    const run = vi.fn(() => HANDLED);
    render(
      <ActionsProvider>
        <KeybindingDispatcher />
        <CommandHandler run={run} />
      </ActionsProvider>,
    );

    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true })).toBe(true);
    expect(run).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true, shiftKey: true })).toBe(false);
    expect(run).toHaveBeenCalledOnce();
  });

  it("re-arms the live dispatcher after a same-tab override", () => {
    const run = vi.fn(() => HANDLED);
    render(
      <ActionsProvider>
        <KeybindingDispatcher />
        <CommandHandler run={run} />
      </ActionsProvider>,
    );
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true })).toBe(false);
    expect(run).toHaveBeenCalledOnce();

    act(() => {
      expect(
        setUserKeybindingRule({
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        }),
      ).toEqual({ ok: true, changed: true });
    });
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(window, { key: "k", ctrlKey: true, shiftKey: true })).toBe(false);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("does not persist an identical product default as a user-precedence override", () => {
    const before = getKeybindingSnapshot();
    expect(
      setUserKeybindingRule({
        id: "workbench.showCommands",
        action: "workbench.action.showCommands",
        sequence: "primary+k",
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: false });
    expect(localStorage.getItem(KEYBINDINGS_STORAGE_KEY)).toBeNull();
    expect(getKeybindingSnapshot()).toBe(before);
  });

  it("does not churn snapshots or storage for an empty reset-all", () => {
    const before = getKeybindingSnapshot();
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    expect(resetAllUserKeybindings()).toEqual({ ok: true, changed: false });
    expect(getKeybindingSnapshot()).toBe(before);
    expect(remove).not.toHaveBeenCalled();
  });

  it("keeps live test subscribers coherent when the cache is reset", async () => {
    render(<Probe />);
    localStorage.setItem(
      KEYBINDINGS_STORAGE_KEY,
      JSON.stringify([
        {
          id: "workbench.showCommands",
          action: "workbench.action.showCommands",
          sequence: "ctrl+shift+k",
          mode: "global",
        },
      ]),
    );
    act(() => resetKeybindingStoreForTesting());
    await waitFor(() =>
      expect(screen.getByTestId("snapshot")).toHaveTextContent(
        "workbench.showCommands:ctrl+shift+k",
      ),
    );
  });

  it("falls back to defaults for malformed storage and removes data on reset-all", () => {
    localStorage.setItem(KEYBINDINGS_STORAGE_KEY, "{");
    resetKeybindingStoreForTesting();
    expect(getKeybindingSnapshot().userRules).toEqual([]);
    expect(getKeybindingSnapshot().effectiveRules.length).toBeGreaterThan(0);
    expect(resetAllUserKeybindings()).toEqual({ ok: true, changed: true });
    expect(localStorage.getItem(KEYBINDINGS_STORAGE_KEY)).toBeNull();
  });
});
