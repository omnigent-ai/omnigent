import { afterEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { SearchIcon } from "lucide-react";

import { PaletteCommandScope } from "./PaletteCommandScope";
import {
  type PaletteCommand,
  usePaletteCommands,
  usePaletteCommandsStore,
} from "./paletteCommands";

const command = (id: string): PaletteCommand => ({
  id,
  label: `Command ${id}`,
  icon: SearchIcon,
  keywords: [],
  run: vi.fn(),
});

const mainCommands = [command("main")];
const sideCommands = [command("side")];

/** A pane that registers the same source as every other pane. */
function Pane({ commands }: { commands: PaletteCommand[] }) {
  usePaletteCommands("pull-request", "Pull request", commands);
  return null;
}

function offered(): string[] {
  return Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) =>
    group.commands.map((entry) => entry.id),
  );
}

function TwoPanes({ focused, side = true }: { focused: "main" | "side"; side?: boolean }) {
  return (
    <>
      <PaletteCommandScope enabled={focused === "main"}>
        <Pane commands={mainCommands} />
      </PaletteCommandScope>
      {side && (
        <PaletteCommandScope enabled={focused === "side"}>
          <Pane commands={sideCommands} />
        </PaletteCommandScope>
      )}
    </>
  );
}

afterEach(() => usePaletteCommandsStore.setState({ groups: {} }));

describe("PaletteCommandScope", () => {
  it("offers only the focused pane's commands as focus moves between panes with the same source", () => {
    const view = render(<TwoPanes focused="main" />);
    expect(offered()).toEqual(["main"]);
    view.rerender(<TwoPanes focused="side" />);
    expect(offered()).toEqual(["side"]);
    view.rerender(<TwoPanes focused="main" />);
    expect(offered()).toEqual(["main"]);
  });

  it("never withdraws the enabled pane's commands when the other one hides or unmounts", () => {
    const view = render(<TwoPanes focused="side" />);
    view.rerender(<TwoPanes focused="main" />);
    expect(offered()).toEqual(["main"]);
    view.rerender(<TwoPanes focused="main" side={false} />);
    expect(offered()).toEqual(["main"]);
    view.rerender(<TwoPanes focused="side" />);
    expect(offered()).toEqual(["side"]);
    view.unmount();
    expect(offered()).toEqual([]);
  });

  it("keeps a scope inside a disabled one disabled", () => {
    const view = render(
      <PaletteCommandScope enabled={false}>
        <PaletteCommandScope enabled>
          <Pane commands={mainCommands} />
        </PaletteCommandScope>
      </PaletteCommandScope>,
    );
    expect(offered()).toEqual([]);
    view.rerender(
      <PaletteCommandScope enabled>
        <PaletteCommandScope enabled>
          <Pane commands={mainCommands} />
        </PaletteCommandScope>
      </PaletteCommandScope>,
    );
    expect(offered()).toEqual(["main"]);
  });

  it("leaves an unscoped source under its own name", () => {
    render(
      <>
        <Pane commands={mainCommands} />
        <PaletteCommandScope enabled>
          <Pane commands={sideCommands} />
        </PaletteCommandScope>
      </>,
    );
    const keys = Object.keys(usePaletteCommandsStore.getState().groups);
    expect(keys).toContain("pull-request");
    expect(keys.filter((key) => key !== "pull-request")).toHaveLength(1);
    expect(offered().sort()).toEqual(["main", "side"]);
  });
});
