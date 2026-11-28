// ⇧J / ⇧K follow the same pane owner as the PR page's ⌘K rows: of two PR pages on screen,
// only the one that owns commands moves its stack, and ownership can move without a remount.

import { cleanup, fireEvent, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PaletteCommandScope } from "@/store/PaletteCommandScope";

import { usePullRequestStackKeys } from "./usePullRequestStackKeys";

afterEach(cleanup);

const pressPrevious = () => fireEvent.keyDown(window, { key: "K", code: "KeyK", shiftKey: true });

/** A pane's stack keys, owning commands as `owns.current` says on each render. */
function mountPane(owns: { current: boolean }) {
  const step = vi.fn();
  const view = renderHook(
    () => usePullRequestStackKeys({ active: true, canStep: () => true, step }),
    {
      wrapper: ({ children }: { children: ReactNode }) => (
        <PaletteCommandScope enabled={owns.current}>{children}</PaletteCommandScope>
      ),
    },
  );
  return { step, rerender: view.rerender };
}

describe("the stack keys and pane ownership", () => {
  it("leave the key alone in a pane that reads but doesn't own commands", () => {
    const pane = mountPane({ current: false });
    expect(pressPrevious()).toBe(true);
    expect(pane.step).not.toHaveBeenCalled();
  });

  it("step only the pane that owns commands, of two live ones", () => {
    const main = mountPane({ current: false });
    const sidebar = mountPane({ current: true });
    expect(pressPrevious()).toBe(false);
    expect(main.step).not.toHaveBeenCalled();
    expect(sidebar.step).toHaveBeenCalledExactlyOnceWith("previous");
  });

  it("follow ownership as it moves, without either pane remounting", () => {
    const mainOwns = { current: true };
    const sidebarOwns = { current: false };
    const main = mountPane(mainOwns);
    const sidebar = mountPane(sidebarOwns);
    pressPrevious();
    expect(main.step).toHaveBeenCalledTimes(1);
    mainOwns.current = false;
    sidebarOwns.current = true;
    main.rerender();
    sidebar.rerender();
    pressPrevious();
    expect(main.step).toHaveBeenCalledTimes(1);
    expect(sidebar.step).toHaveBeenCalledExactlyOnceWith("previous");
  });
});
