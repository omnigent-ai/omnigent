import { describe, expect, it, vi } from "vitest";

import { type SplitPaneCommands, sessionPaneStrip } from "./paneMenu";

const menu = (commands: Omit<SplitPaneCommands, "zoomed"> | null, splitting = false) => ({
  onSplit: vi.fn(),
  splitting,
  commands: () => (commands ? { zoomed: false, ...commands } : null),
});

describe("sessionPaneStrip", () => {
  it("names a pane that can't split, with nothing to run", () => {
    expect(sessionPaneStrip("agent", undefined)).toEqual({ name: "agent", commands: null });
  });

  it("offers a lone pane's Split as its menu does, with no Maximize or Close", () => {
    const paneMenu = menu(null);
    const { commands } = sessionPaneStrip("agent", paneMenu);
    expect(Object.keys(commands ?? {}).sort()).toEqual(["splitDown", "splitRight"]);
    commands?.splitRight?.();
    commands?.splitDown?.();
    expect(paneMenu.onSplit.mock.calls).toEqual([["right"], ["down"]]);
  });

  it("adds the split's Maximize and Close, and leaves Close out for the agent's pane", () => {
    const close = vi.fn();
    const shell = sessionPaneStrip("bash", menu({ toggleZoom: vi.fn(), close }));
    expect(shell.commands?.close).toBe(close);
    expect(shell.commands?.toggleZoom).toBeDefined();
    const agent = sessionPaneStrip("agent", menu({ toggleZoom: vi.fn() }));
    expect(agent.commands?.close).toBeUndefined();
  });

  it("passes the split's swaps and Move Pane spots to the row's menu", () => {
    const swaps = [{ id: "terminal.swapLeft", label: "Swap with Pane Left", run: vi.fn() }];
    const moveOptions = () => [];
    const { commands } = sessionPaneStrip(
      "bash",
      menu({ toggleZoom: vi.fn(), swaps, moveOptions }),
    );
    expect(commands?.swaps).toBe(swaps);
    expect(commands?.moveOptions).toBe(moveOptions);
    const zoomed = sessionPaneStrip("bash", menu({ toggleZoom: vi.fn() }));
    expect(zoomed.commands?.swaps).toBeUndefined();
    expect(zoomed.commands?.moveOptions).toBeUndefined();
  });

  it("holds Split while a shell is starting", () => {
    const { commands } = sessionPaneStrip("agent", menu(null, true));
    expect(commands?.splitRight).toBeUndefined();
    expect(commands).toEqual({});
  });
});
