import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MainTerminalSurface } from "./MainTerminalSurface";

const gate = vi.hoisted(() => ({ panesView: false }));
vi.mock("./panesViewGate", () => ({ usePanesView: () => gate.panesView }));
// Each view reports the props it was given.
vi.mock("./MainTerminalView", () => ({
  MainTerminalView: (props: object) => (
    <div data-testid="terminal-view" data-props={JSON.stringify(props)} />
  ),
}));
const loaded = vi.hoisted(() => ({ panesView: false, fails: false }));
vi.mock("./SplitPanesMainView", () => {
  loaded.panesView = true;
  return {
    SplitPanesMainView: (props: object) => {
      if (loaded.fails) throw new Error("panes view failed");
      return <div data-testid="panes-view" data-props={JSON.stringify(props)} />;
    },
  };
});

const propsOf = (testId: string) =>
  JSON.parse(screen.getByTestId(testId).dataset.props ?? "{}") as Record<string, unknown>;

function Surface() {
  return (
    <MainTerminalSurface
      conversationId="conv_surface"
      initialTerminalKey={null}
      visible
      readOnly={false}
      splittable
      chatShown
    />
  );
}

afterEach(() => {
  gate.panesView = false;
  loaded.fails = false;
  vi.restoreAllMocks();
});

describe("MainTerminalSurface", () => {
  it("shows the Terminal view as before where the panes view is off, never loading it", () => {
    render(<Surface />);
    expect(screen.queryByTestId("panes-view")).toBeNull();
    expect(loaded.panesView).toBe(false);
    expect(propsOf("terminal-view")).toEqual({
      conversationId: "conv_surface",
      initialTerminalKey: null,
      visible: true,
      readOnly: false,
      splittable: true,
    });
  });

  it("loads the panes view where it's on, told whether Chat shows and never the old splits", async () => {
    gate.panesView = true;
    render(<Surface />);
    // Nothing shows while it loads, so no terminal attaches before the panes do.
    expect(screen.queryByTestId("terminal-view")).toBeNull();
    await screen.findByTestId("panes-view");
    expect(screen.queryByTestId("terminal-view")).toBeNull();
    expect(propsOf("panes-view")).toEqual({
      conversationId: "conv_surface",
      initialTerminalKey: null,
      visible: true,
      readOnly: false,
      chatShown: true,
    });
  });

  it("falls back to the Terminal view when the panes view fails, leaving the page as it is", async () => {
    gate.panesView = true;
    loaded.fails = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Surface />);
    await screen.findByTestId("terminal-view");
    expect(screen.queryByTestId("panes-view")).toBeNull();
    expect(propsOf("terminal-view")).toMatchObject({
      conversationId: "conv_surface",
      splittable: true,
    });
  });
});
