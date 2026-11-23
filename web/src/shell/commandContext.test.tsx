import { act, fireEvent, render, screen } from "@testing-library/react";
import { Profiler, StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  CHECKING_ACCESS_REASON,
  type CommandContext,
  CommandContextProvider,
  type CommandSession,
  commandRegionProps,
  commandTargetKey,
  commandViewForPath,
  regionOfFocusTarget,
  sessionCommandAvailability,
  useCommandContext,
  useCommandRegion,
  useCommandRegionReader,
  usePublishSessionSelection,
} from "./commandContext";

function session(overrides: Partial<CommandSession> = {}): CommandSession {
  return {
    id: "conv_1",
    permissionLevel: 4,
    permissionResolved: true,
    topLevel: true,
    archived: false,
    ...overrides,
  };
}

describe("sessionCommandAvailability", () => {
  it("hides session commands without a session", () => {
    expect(sessionCommandAvailability(null, "edit", "Needs edit")).toEqual({ available: false });
  });

  it("greys out commands needing access until the snapshot confirms it", () => {
    // A list row without a level reads as the owner, so it can't grant access on its own.
    const provisional = session({ permissionLevel: null, permissionResolved: false });
    expect(sessionCommandAvailability(provisional, "edit", "Needs edit")).toEqual({
      available: false,
      reason: CHECKING_ACCESS_REASON,
    });
    expect(sessionCommandAvailability(provisional, "view", "Needs view")).toEqual({
      available: true,
    });
  });

  it.each([
    ["edit", 1, false],
    ["edit", 2, true],
    ["owner", 2, false],
    ["owner", 4, true],
    // Single-user servers report no level, which the server treats as full access.
    ["owner", null, true],
  ] as const)("%s access at level %s is available: %s", (access, level, expected) => {
    const availability = sessionCommandAvailability(
      session({ permissionLevel: level }),
      access,
      "Denied",
    );
    expect(availability).toEqual(
      expected ? { available: true } : { available: false, reason: "Denied" },
    );
  });
});

describe("commandViewForPath", () => {
  it.each([
    ["/c/conv_1", "session"],
    ["/", "new-session"],
    ["/inbox", "inbox"],
    ["/tasks/task_1", "automations"],
    ["/settings/appearance", "settings"],
    ["/members", "other"],
    ["/inboxes", "other"],
  ] as const)("%s is %s", (path, view) => {
    expect(commandViewForPath(path)).toBe(view);
  });

  it("reads paths under the app's base path", () => {
    const rebase = (path: string) => `/apps/omnigent${path === "/" ? "" : path}`;
    expect(commandViewForPath("/apps/omnigent/c/conv_1", rebase)).toBe("session");
    expect(commandViewForPath("/apps/omnigent", rebase)).toBe("new-session");
    expect(commandViewForPath("/c/conv_1", rebase)).toBe("other");
  });
});

describe("commandTargetKey", () => {
  const base: CommandContext = {
    session: session(),
    view: "session",
    workspaceTab: "files",
    selection: { sessionIds: [], filePath: "a.ts" },
  };
  const key = (context: CommandContext) => commandTargetKey(context, "conversation");

  it("ignores a refreshed access level and changes with what commands act on", () => {
    expect(key({ ...base, session: session({ permissionLevel: 2 }) })).toBe(key(base));
    for (const changed of [
      { ...base, session: session({ id: "conv_2" }) },
      { ...base, workspaceTab: "changes" as const },
      { ...base, selection: { sessionIds: ["conv_3"], filePath: "a.ts" } },
      { ...base, selection: { sessionIds: [], filePath: "b.ts" } },
    ]) {
      expect(key(changed)).not.toBe(key(base));
    }
    expect(commandTargetKey(base, "sidebar")).not.toBe(key(base));
    expect(commandTargetKey(base, null)).not.toBe(key(base));
  });
});

describe("regionOfFocusTarget", () => {
  it("finds the marked region, and ignores focus inside dialogs and menus", () => {
    document.body.innerHTML = `
      <aside data-command-region="sidebar"><button id="row">Row</button></aside>
      <div role="dialog"><input id="palette" /></div>
      <div role="menu"><button id="item">Item</button></div>
      <button id="loose">Loose</button>`;
    expect(regionOfFocusTarget(document.getElementById("row"))).toBe("sidebar");
    expect(regionOfFocusTarget(document.getElementById("loose"))).toBeNull();
    expect(regionOfFocusTarget(document.getElementById("palette"))).toBeUndefined();
    expect(regionOfFocusTarget(document.getElementById("item"))).toBeUndefined();
    document.body.innerHTML = "";
  });

  it("finds a workspace sheet's region, though it's a dialog", () => {
    document.body.innerHTML = `
      <div role="dialog" data-workspace-sheet data-command-region="workspace">
        <button id="tab">Tab</button>
      </div>`;
    expect(regionOfFocusTarget(document.getElementById("tab"))).toBe("workspace");
    document.body.innerHTML = "";
  });
});

describe("CommandContextProvider", () => {
  function ShowContext() {
    const context = useCommandContext();
    const region = useCommandRegion();
    return (
      <output>
        {region ?? "none"}|{context.selection.sessionIds.join(",")}
      </output>
    );
  }

  function Selection({ ids }: { ids: ReadonlySet<string> }) {
    usePublishSessionSelection(ids);
    return null;
  }

  it("tracks the focused region and the published selection, and clears it on unmount", () => {
    const { rerender } = render(
      <CommandContextProvider session={null} view="other" workspaceTab={null} filePath={null}>
        <aside {...commandRegionProps("sidebar")}>
          <button type="button">Row</button>
        </aside>
        <div role="dialog">
          <input aria-label="Palette" />
        </div>
        <Selection ids={new Set(["conv_1", "conv_2"])} />
        <ShowContext />
      </CommandContextProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("none|conv_1,conv_2");
    act(() => fireEvent.focusIn(screen.getByRole("button", { name: "Row" })));
    expect(screen.getByRole("status")).toHaveTextContent("sidebar|");
    // Opening the palette keeps the region it was opened from.
    act(() => fireEvent.focusIn(screen.getByRole("textbox", { name: "Palette" })));
    expect(screen.getByRole("status")).toHaveTextContent("sidebar|");

    rerender(
      <CommandContextProvider session={null} view="other" workspaceTab={null} filePath={null}>
        <ShowContext />
      </CommandContextProvider>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("sidebar|");
    expect(screen.getByRole("status")).not.toHaveTextContent("conv_1");
  });

  /** Two regions with two buttons each, and a reader of each part of the command context. */
  function Regions({ renders }: { renders: Record<string, number> }) {
    const count = (id: string) => () => {
      renders[id] = (renders[id] ?? 0) + 1;
    };
    return (
      <>
        <aside {...commandRegionProps("sidebar")}>
          <button type="button">Row</button>
          <button type="button">Other row</button>
        </aside>
        <section {...commandRegionProps("workspace")}>
          <button type="button">File</button>
        </section>
        <Profiler id="context" onRender={count("context")}>
          <ContextOnly />
        </Profiler>
        <Profiler id="region" onRender={count("region")}>
          <RegionOnly />
        </Profiler>
        <Profiler id="closed" onRender={count("closed")}>
          <RegionOnly when={false} />
        </Profiler>
        <LiveRegion />
      </>
    );
  }
  function ContextOnly() {
    return <span>{useCommandContext().view}</span>;
  }
  function RegionOnly({ when = true }: { when?: boolean }) {
    return <span data-testid={when ? "region" : "closed"}>{useCommandRegion(when) ?? "none"}</span>;
  }
  let readRegion: () => string | null = () => null;
  function LiveRegion() {
    readRegion = useCommandRegionReader();
    return null;
  }
  const provider = (renders: Record<string, number>) => (
    <CommandContextProvider session={null} view="other" workspaceTab={null} filePath={null}>
      <Regions renders={renders} />
    </CommandContextProvider>
  );
  const focus = (name: string) =>
    act(() => fireEvent.focusIn(screen.getByRole("button", { name })));

  it("re-renders only what reads the region when focus moves to another region", () => {
    const renders: Record<string, number> = {};
    render(provider(renders));
    const before = { ...renders };
    focus("Row");
    focus("File");
    expect(screen.getByTestId("region")).toHaveTextContent("workspace");
    expect(renders.region).toBe((before.region ?? 0) + 2);
    expect(renders.context).toBe(before.context);
    // A closed reader doesn't follow focus; the live reader sees it at once.
    expect(renders.closed).toBe(before.closed);
    expect(screen.getByTestId("closed")).toHaveTextContent("none");
    expect(readRegion()).toBe("workspace");
  });

  it("notifies no one when focus moves within the same region", () => {
    const renders: Record<string, number> = {};
    render(provider(renders));
    focus("Row");
    const after = { ...renders };
    focus("Other row");
    expect(renders).toEqual(after);
    expect(readRegion()).toBe("sidebar");
  });

  it("follows focus with one listener per mounted provider, and none after it unmounts", () => {
    const added = vi.spyOn(document, "addEventListener");
    const removed = vi.spyOn(document, "removeEventListener");
    const view = render(<StrictMode>{provider({})}</StrictMode>);
    focus("File");
    expect(screen.getByTestId("region")).toHaveTextContent("workspace");
    view.unmount();
    const focusins = (spy: typeof added) =>
      spy.mock.calls.filter(([type]) => type === "focusin").length;
    expect(focusins(removed)).toBe(focusins(added));
    // A new provider starts from its own store, in no region.
    render(provider({}));
    expect(screen.getByTestId("region")).toHaveTextContent("none");
    added.mockRestore();
    removed.mockRestore();
  });
});
