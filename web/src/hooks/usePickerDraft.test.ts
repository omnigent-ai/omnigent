// The picker draft hook: checks made while open, sent once as one change when the picker closes.

import { act, renderHook } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import { describe, expect, it, vi } from "vitest";

import type { PickerDraftChange } from "@/lib/pickerDraft";
import { usePickerDraft } from "./usePickerDraft";

const PR = "https://github.com/acme/app/pull/1";
const keyOf = (name: string) => name;

/** What's chosen on the PR, as a store whose every read after a change is a new set. */
function chosenStore(initial: string[]) {
  let chosen: ReadonlySet<string> = new Set(initial);
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => chosen,
    /** The same items again, as a new set, as a pending change's arrival re-derives them. */
    touch: () => {
      chosen = new Set(chosen);
      for (const listener of listeners) listener();
    },
  };
}

function renderDraft(send: (change: PickerDraftChange<string>) => void, initial: string[] = []) {
  const store = chosenStore(initial);
  const view = renderHook(
    ({ open }: { open: boolean }) => {
      const current = useSyncExternalStore(store.subscribe, store.read);
      return usePickerDraft({ prUrl: PR, current, open, waiting: false, keyOf, send });
    },
    { initialProps: { open: true } },
  );
  return { ...view, store };
}

describe("usePickerDraft", () => {
  it("sends nothing while open, then one change once closed", () => {
    const send = vi.fn();
    const view = renderDraft(send, ["bug"]);
    act(() => view.result.current.toggle("ready"));
    act(() => view.result.current.toggle("bug"));
    expect(view.result.current.change).toEqual({ prUrl: PR, add: ["ready"], remove: ["bug"] });
    expect(send).not.toHaveBeenCalled();
    view.rerender({ open: false });
    expect(send).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledWith({ prUrl: PR, add: ["ready"], remove: ["bug"] });
    expect(view.result.current.change).toBeNull();
  });

  it("sends a closed draft once, even when sending re-renders it before the draft clears", () => {
    let touch = () => {};
    // Sending registers the change, which re-derives what's chosen before the reset lands.
    const send = vi.fn(() => touch());
    const view = renderDraft(send);
    touch = view.store.touch;
    act(() => view.result.current.toggle("ready"));
    act(() => view.rerender({ open: false }));
    expect(send).toHaveBeenCalledOnce();
  });
});
