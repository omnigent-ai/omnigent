// The palette's feature rows load only where the sidebar shows as tabs: the
// workspace_sidebar_v2 flag on, in a client that isn't driven by touch.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

import type { FeaturePaletteRow } from "./featurePaletteRows";

/** The part of a palette command that accepting a row reads. */
interface PaletteCommandStub {
  run: () => void;
  keepsPaletteOpen?: boolean;
  movesFocus?: boolean;
}

// One rows array, as the real source keeps until the offered commands change.
const loads = vi.hoisted(() => ({
  count: 0,
  info: null as ServerInfo | null,
  rows: [] as never[],
  // The commands the rows offer now, by row id.
  commands: {} as Record<string, PaletteCommandStub>,
}));
/** The rows module, counting each time a fresh loader takes its source. */
function rowsModule() {
  return {
    get featurePaletteSource() {
      loads.count += 1;
      return {
        subscribe: () => () => {},
        rows: () => loads.rows,
        command: (rowId: string) => loads.commands[rowId] ?? null,
      };
    },
  };
}

// The factory may be cached across `vi.resetModules`, so the count sits in the getter.
vi.mock("./featurePaletteRows", () => rowsModule());
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => loads.info }));

const withFlag = (enabled: boolean): ServerInfo => ({
  ...FALLBACK_SERVER_INFO,
  features: { ...FALLBACK_SERVER_INFO.features, workspace_sidebar_v2: enabled },
});

/** The window is `width` wide, and its primary pointer is a mouse or a finger. */
function setViewport(width: number, pointer: "fine" | "coarse" = "fine") {
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => {
    const max = /max-width:\s*([\d.]+)px/.exec(query);
    const min = /min-width:\s*([\d.]+)px/.exec(query);
    const pointerQuery = /pointer:\s*(\w+)/.exec(query);
    const matches =
      pointerQuery !== null
        ? pointerQuery[1] === pointer
        : (max === null || width <= Number(max[1])) && (min === null || width >= Number(min[1]));
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    } as MediaQueryList;
  });
}

/** Render the hook from a fresh module, so each case starts with nothing loaded. */
async function renderPaletteCommands(open: boolean) {
  const { useFeaturePaletteCommands } = await import("./useFeaturePaletteCommands");
  return renderHook(({ isOpen }) => useFeaturePaletteCommands(isOpen), {
    initialProps: { isOpen: open },
  });
}

/** Let a started load settle, so a load that shouldn't happen has had its chance. */
async function settle() {
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
}

beforeEach(() => {
  vi.resetModules();
  loads.count = 0;
  loads.commands = {};
});
afterEach(() => {
  delete (window as unknown as { omnigentNative?: unknown }).omnigentNative;
  vi.restoreAllMocks();
});

describe("useFeaturePaletteCommands", () => {
  it.each([
    ["a desktop window", 1440],
    ["a narrow desktop window", 700],
  ])("loads the rows in %s with the flag on, before the palette opens", async (_, width) => {
    loads.info = withFlag(true);
    setViewport(width);
    await renderPaletteCommands(false);
    await vi.waitFor(() => expect(loads.count).toBe(1));
  });

  it.each([
    ["a phone's browser with the flag on", withFlag(true), 390, "coarse", null],
    ["a wide tablet's browser with the flag on", withFlag(true), 1366, "coarse", null],
    ["the iOS app on a wide iPad with the flag on", withFlag(true), 1366, "fine", "ios"],
    ["a desktop window with the flag off", withFlag(false), 1440, "fine", null],
  ] as const)("loads nothing in %s, open or closed", async (_, info, width, pointer, shell) => {
    loads.info = info;
    setViewport(width, pointer);
    if (shell) {
      (window as unknown as { omnigentNative?: { kind: string } }).omnigentNative = {
        kind: shell,
      };
    }
    const closed = await renderPaletteCommands(false);
    const open = await renderPaletteCommands(true);
    await settle();
    expect(loads.count).toBe(0);
    expect(closed.result.current.rows).toEqual([]);
    expect(open.result.current.rows).toEqual([]);
  });

  it("lists no rows when they can't load, and loads them on the next try", async () => {
    loads.info = withFlag(true);
    setViewport(1440);
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.doMock("./featurePaletteRows", () => {
      throw new Error("Failed to fetch dynamically imported module");
    });
    const { useFeaturePaletteCommands } = await import("./useFeaturePaletteCommands");
    const failed = renderHook(() => useFeaturePaletteCommands(true));
    await settle();
    expect(failed.result.current.rows).toEqual([]);
    failed.unmount();

    vi.doMock("./featurePaletteRows", () => rowsModule());
    renderHook(() => useFeaturePaletteCommands(true));
    await vi.waitFor(() => expect(loads.count).toBe(1));
  });

  describe("accepting a row", () => {
    /** The loaded hook, and a log of the palette closing and each command running. */
    async function acceptingRows() {
      loads.info = withFlag(true);
      setViewport(1440);
      const events: string[] = [];
      const command = (name: string, extra: Partial<PaletteCommandStub> = {}) => ({
        run: () => events.push(`run ${name}`),
        ...extra,
      });
      loads.commands = {
        plain: command("plain"),
        stays: command("stays", { keepsPaletteOpen: true }),
        focus: command("focus", { movesFocus: true }),
      };
      const hook = await renderPaletteCommands(true);
      await vi.waitFor(() => expect(loads.count).toBe(1));
      await settle();
      const accept = (rowId: string) =>
        hook.result.current.accept({ id: rowId } as FeaturePaletteRow, () => events.push("close"));
      return { hook, events, accept };
    }

    it("closes the palette, then runs the command, or runs it in place when it keeps the palette open", async () => {
      const { events, accept } = await acceptingRows();
      accept("plain");
      accept("stays");
      expect(events).toEqual(["close", "run plain", "run stays"]);
    });

    it("runs a command that moves focus once, after the palette has handed focus back", async () => {
      const { hook, events, accept } = await acceptingRows();
      accept("focus");
      expect(events).toEqual(["close"]);

      hook.result.current.onCloseAutoFocus();
      expect(events).toEqual(["close"]);
      await Promise.resolve();
      expect(events).toEqual(["close", "run focus"]);

      hook.result.current.onCloseAutoFocus();
      await Promise.resolve();
      expect(events).toEqual(["close", "run focus"]);
    });

    it("drops a focus-moving command when the palette reopens before handing focus back", async () => {
      const { hook, events, accept } = await acceptingRows();
      accept("focus");
      hook.rerender({ isOpen: false });
      // Reopened during the exit animation: the old content's close never reaches the hook.
      hook.rerender({ isOpen: true });
      hook.rerender({ isOpen: false });
      hook.result.current.onCloseAutoFocus();
      await Promise.resolve();
      expect(events).toEqual(["close"]);
    });

    it("only closes the palette for a row whose command was withdrawn", async () => {
      const { events, accept } = await acceptingRows();
      accept("withdrawn");
      expect(events).toEqual(["close"]);
    });
  });
});
