import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StackLandingDiagnostic } from "@/lib/pullRequestStacks/stackLanding";
import { landingStack, member, pr, url } from "@/lib/pullRequestStacks/stackLandingFixtures";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import type { PullRequestStackDiff } from "../stackDiff/usePullRequestStackDiff";
import {
  PullRequestStackLandingNotice,
  StackLandingPaletteCommands,
} from "./PullRequestStackLanding";
import { stackLanding } from "./stackLandingCommands";
import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Stack descriptions in an example format, which the page reads once a build registers it.
withPullRequestIntegrations({ stackFormats: [EXAMPLE_STACK_FORMAT] });

const clipboard = vi.hoisted(() => ({ copied: [] as string[] }));
vi.mock("@/lib/clipboard", () => ({
  copyText: (text: string) => {
    clipboard.copied.push(text);
    return Promise.resolve();
  },
}));

const merged = () =>
  landingStack({ members: [member(100), member(101, { state: "MERGED" }), member(102)] });

function diffWith(diagnostic: StackLandingDiagnostic | null): PullRequestStackDiff {
  return {
    parent: { number: 101, label: "#101", headRepository: "acme/app" },
    intent: "all",
    choose: vi.fn(),
    effective: { kind: "all", fallback: null },
    diagnostic,
    reload: vi.fn(),
    retry: vi.fn(),
  };
}

const paletteCommands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

beforeEach(() => {
  clipboard.copied = [];
});
afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("PullRequestStackLandingNotice", () => {
  it("asks for the parent first, and copies the land command from the same definition as ⌘K", async () => {
    const landing = stackLanding(landingStack(), pr(), null);
    render(
      <>
        <PullRequestStackLandingNotice landing={landing} />
        <StackLandingPaletteCommands landing={landing} active />
      </>,
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      "Merge #101 first. A stack lands in order, starting with its first PR, and stk land does that for you.",
    );
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Copy land command" })),
    );
    const fromPalette = paletteCommands().find(
      (command) => command.label === "Copy stk land command",
    )!;
    await act(async () => fromPalette.run());
    expect(clipboard.copied).toEqual([
      "git switch -- 'stack/102' && stk land",
      "git switch -- 'stack/102' && stk land",
    ]);
  });

  it("asks to sync a squash-merged parent, and copies the sync command", async () => {
    const landing = stackLanding(merged(), pr(), diffWith({ kind: "separable" }));
    render(<PullRequestStackLandingNotice landing={landing} />);
    expect(screen.getByRole("note")).toHaveTextContent(
      "#101 is merged, but this PR still includes its commits. Run stk sync && stk push before you merge.",
    );
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy command" })));
    expect(clipboard.copied).toEqual(["stk sync && stk push"]);
  });

  it("reloads a stale check, and asks a failed one again, only when the person asks", () => {
    const stale = diffWith({ kind: "stale" });
    render(<PullRequestStackLandingNotice landing={stackLanding(merged(), pr(), stale)} />);
    expect(screen.getByRole("note")).toHaveTextContent("This stack changed since it was checked.");
    expect(stale.reload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reload stack status" }));
    expect(stale.reload).toHaveBeenCalledOnce();
    cleanup();
    const failed = diffWith({ kind: "failed" });
    render(<PullRequestStackLandingNotice landing={stackLanding(merged(), pr(), failed)} />);
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(failed.retry).toHaveBeenCalledOnce();
  });

  it("shows nothing for a synced stack, or one it can't vouch for", () => {
    const synced = stackLanding(merged(), pr(), diffWith({ kind: "not_carried" }));
    const older = stackLanding(
      landingStack({
        members: [member(100), member(101), member(102, { base_default_branch: undefined })],
      }),
      pr(),
      null,
    );
    render(
      <>
        <PullRequestStackLandingNotice landing={synced} />
        <PullRequestStackLandingNotice landing={older} />
      </>,
    );
    expect(screen.queryByRole("note")).toBeNull();
    expect(older.land).toBeNull();
  });
});

describe("StackLandingPaletteCommands", () => {
  it("offers the land and check commands for the stack's first PR too", async () => {
    const root = stackLanding(
      landingStack({ self: 100 }),
      pr({ url: url(100), head_ref: "stack/100" }),
      null,
    );
    render(<StackLandingPaletteCommands landing={root} active />);
    expect(paletteCommands().map((command) => command.label)).toEqual([
      "Copy stk land command",
      "Copy stk land --check command",
    ]);
    await act(async () => paletteCommands()[1]!.run());
    expect(clipboard.copied).toEqual(["git switch -- 'stack/100' && stk land --check"]);
  });

  it("quotes an odd branch name in what it copies", async () => {
    const landing = stackLanding(landingStack(), pr({ head_ref: "føo's$(x)" }), null);
    render(<StackLandingPaletteCommands landing={landing} active />);
    await act(async () => paletteCommands()[0]!.run());
    expect(clipboard.copied).toEqual([`git switch -- 'føo'"'"'s$(x)' && stk land`]);
  });

  it("offers nothing while the page is hidden", () => {
    render(
      <StackLandingPaletteCommands
        landing={stackLanding(landingStack(), pr(), null)}
        active={false}
      />,
    );
    expect(paletteCommands()).toEqual([]);
  });
});
