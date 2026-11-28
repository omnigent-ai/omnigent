import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { StackDiffPaletteCommands, StackStatusPaletteCommands } from "./StackDiffPaletteCommands";
import { STACK_DIFF_CHOICES } from "./stackDiffCommands";
import { comparison } from "./stackDiffFixtures";
import type { EffectiveStackDiff, StackDiffIntent } from "./stackDiffMode";
import type { PullRequestStackDiff } from "./usePullRequestStackDiff";

function diffOf(
  effective: EffectiveStackDiff,
  intent: StackDiffIntent = "automatic",
): PullRequestStackDiff {
  return {
    parent: { number: 100, label: "#100", headRepository: "acme/app" },
    intent,
    choose: vi.fn(),
    effective,
    diagnostic: null,
    reload: vi.fn(),
    retry: vi.fn(),
  };
}

const paletteCommands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("StackDiffPaletteCommands", () => {
  it("offers the other choices under the menu's definitions, and shows Changes", () => {
    const diff = diffOf({ kind: "this-pr", comparison: comparison() }, "this-pr");
    const showChanges = vi.fn();
    render(<StackDiffPaletteCommands diff={diff} active showChanges={showChanges} />);
    const labels = paletteCommands().map((command) => command.label);
    expect(labels).toEqual(
      STACK_DIFF_CHOICES.filter((choice) => choice.intent !== "this-pr").map(
        (choice) => choice.paletteLabel,
      ),
    );
    const all = paletteCommands().find(
      (command) => command.label === "Show all of the pull request's changes",
    )!;
    act(() => all.run());
    expect(diff.choose).toHaveBeenCalledWith("all");
    expect(showChanges).toHaveBeenCalledOnce();
  });

  it("offers nothing where no stack diff is offered, or while the page is hidden", () => {
    render(<StackDiffPaletteCommands diff={null} active showChanges={() => undefined} />);
    render(
      <StackDiffPaletteCommands
        diff={diffOf({ kind: "all", fallback: null })}
        active={false}
        showChanges={() => undefined}
      />,
    );
    expect(paletteCommands()).toEqual([]);
  });
});

describe("StackStatusPaletteCommands", () => {
  it("offers Reload while a comparison is stale, on Changes or for Summary's check", () => {
    const stale = diffOf({ kind: "stale" });
    render(<StackStatusPaletteCommands diff={stale} active />);
    const reload = paletteCommands().find((command) => command.label === "Reload stack status")!;
    act(() => reload.run());
    expect(stale.reload).toHaveBeenCalledOnce();
    cleanup();
    usePaletteCommandsStore.setState({ groups: {} });
    const summary: PullRequestStackDiff = {
      ...diffOf({ kind: "all", fallback: null }),
      diagnostic: { kind: "stale" },
    };
    render(<StackStatusPaletteCommands diff={summary} active />);
    expect(paletteCommands().map((command) => command.label)).toEqual(["Reload stack status"]);
  });

  it("offers Try Again after a chosen comparison failed, and runs the diff's retry", () => {
    const failed = diffOf({ kind: "all", fallback: "failed" }, "this-pr");
    render(<StackStatusPaletteCommands diff={failed} active />);
    const retry = paletteCommands().find((command) => command.label === "Compare with #100 again")!;
    act(() => retry.run());
    expect(failed.retry).toHaveBeenCalledOnce();
  });

  it("offers nothing while nothing is stale, or the page is hidden", () => {
    render(<StackStatusPaletteCommands diff={diffOf({ kind: "all", fallback: null })} active />);
    render(<StackStatusPaletteCommands diff={diffOf({ kind: "stale" })} active={false} />);
    render(<StackStatusPaletteCommands diff={null} active />);
    // A fallback other than a failure has nothing to try again.
    render(
      <StackStatusPaletteCommands
        diff={diffOf({ kind: "all", fallback: "parent_closed" }, "this-pr")}
        active
      />,
    );
    expect(paletteCommands()).toEqual([]);
  });
});
