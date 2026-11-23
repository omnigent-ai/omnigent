import { act, cleanup, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalLayout } from "@/lib/paneSplitLayout";
import type { CommandAvailability } from "@/store/paletteCommands";
import { installResizableWidth } from "@/test/resizableWidth";

import { NO_ROOM_TO_MOVE_REASON } from "./paneMove";
import type { PaneSizing } from "./paneSizing";
import { usePaneMoveAvailability } from "./usePaneMoveAvailability";

const SIDE_BY_SIDE: TerminalLayout = { axis: "columns", ratio: 0.5, children: ["a", "b"] };

/** A rule with room to move a pane only while the panes are at least 400 wide. */
const sizingAt = (width: () => number): PaneSizing => ({
  fits: () => width() >= 400,
  keeps: () => width() >= 400,
  toward: (_target, from) => from,
  refusal: () => "no room",
});

let latest: CommandAvailability | null = null;

function Panes({ width }: { width: () => number }) {
  const rootRef = useRef<HTMLDivElement>(null);
  latest = usePaneMoveAvailability({
    layout: SIDE_BY_SIDE,
    zoomedKey: null,
    paneKey: "a",
    sizing: () => sizingAt(width),
    rootRef,
  });
  return (
    <div ref={rootRef}>
      <div data-terminal-pane="a" data-terminal-cols="80" />
      <div data-terminal-pane="b" />
    </div>
  );
}

afterEach(() => {
  cleanup();
  latest = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("usePaneMoveAvailability", () => {
  it("measures again as the panes resize from their first mount", () => {
    let width = 800;
    const resize = installResizableWidth(width);
    render(<Panes width={() => width} />);
    expect(latest).toEqual({ available: true });

    width = 200;
    resize(width);
    expect(latest).toEqual({ available: false, reason: NO_ROOM_TO_MOVE_REASON });
    width = 800;
    resize(width);
    expect(latest).toEqual({ available: true });
  });

  it("measures again as a terminal fits, and stops watching once unmounted", async () => {
    let width = 800;
    installResizableWidth(width);
    const view = render(<Panes width={() => width} />);
    const fitted = view.container.querySelector<HTMLElement>("[data-terminal-cols]")!;
    width = 200;
    await act(async () => fitted.setAttribute("data-terminal-cols", "40"));
    expect(latest).toEqual({ available: false, reason: NO_ROOM_TO_MOVE_REASON });

    const observers = vi.spyOn(MutationObserver.prototype, "disconnect");
    view.unmount();
    expect(observers).toHaveBeenCalled();
  });
});
