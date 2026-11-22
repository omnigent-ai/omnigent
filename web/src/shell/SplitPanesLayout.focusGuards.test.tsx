// When a split's pane may take the keyboard on its own: once as it is split off, never as it
// comes back with the view.

import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { useSplitPaneFocusGuards } from "@/shell/SplitPanesLayout";

const splitOf = (...children: string[]): TerminalSplitState => ({
  layout: { axis: "columns", ratio: 0.5, children: children as [string, string] },
  focusedKey: children[0],
});

interface Props {
  conversationId?: string;
  visible?: boolean;
  split?: TerminalSplitState;
  shownPaneKeys: readonly string[];
}

function renderGuards(initial: Props) {
  const panesRef = { current: null as HTMLDivElement | null };
  return renderHook(
    ({ conversationId = "c1", visible = true, split, shownPaneKeys }: Props) =>
      useSplitPaneFocusGuards({ conversationId, visible, split, shownPaneKeys, panesRef }),
    { initialProps: initial },
  );
}

describe("useSplitPaneFocusGuards", () => {
  it("grants no connect focus to the panes restored with the view", () => {
    const { result } = renderGuards({ split: splitOf("a", "b"), shownPaneKeys: ["a", "b"] });
    expect(result.current.takeConnectFocus("a")).toBe(false);
    expect(result.current.takeConnectFocus("b")).toBe(false);
  });

  it("grants a pane split off connect focus exactly once", () => {
    const { result, rerender } = renderGuards({ shownPaneKeys: ["a"] });
    rerender({ split: splitOf("a", "b"), shownPaneKeys: ["a", "b"] });
    expect(result.current.takeConnectFocus("a")).toBe(false);
    expect(result.current.takeConnectFocus("b")).toBe(true);
    expect(result.current.takeConnectFocus("b")).toBe(false);
    // A later render with the same panes grants nothing more.
    rerender({ split: splitOf("a", "b"), shownPaneKeys: ["a", "b"] });
    expect(result.current.takeConnectFocus("b")).toBe(false);
  });

  it("grants nothing when another conversation's split shows", () => {
    const { result, rerender } = renderGuards({ shownPaneKeys: ["a"] });
    rerender({ conversationId: "c2", split: splitOf("c", "d"), shownPaneKeys: ["c", "d"] });
    expect(result.current.takeConnectFocus("c")).toBe(false);
    expect(result.current.takeConnectFocus("d")).toBe(false);
  });
});
