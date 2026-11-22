import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as CapabilitiesModule from "@/lib/capabilities";
import type { TerminalInfo } from "@/lib/terminals";

import {
  forgetTerminalEnding,
  noteTerminalAttachClosed,
  noteTerminalDeleted,
  outcomeFromCloseReason,
  outcomeFromExitPayload,
  readTerminalEnding,
  recordTerminalEnding,
  resetTerminalEndingsForTests,
  terminalEndingsReported,
  useSessionTerminalEndings,
  useTerminalEnding,
} from "./terminalEndings";

// The server's capabilities; undefined while they're still loading.
const server = vi.hoisted(() => ({ reports: true as boolean | undefined }));
vi.mock("@/lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  getCachedServerInfo: () =>
    server.reports === undefined
      ? undefined
      : { features: { terminal_report_exit_outcome: server.reports } },
}));

const ROW: TerminalInfo = { id: "terminal_zsh_u-1", name: "zsh", session: "u-1", running: true };

beforeEach(() => {
  server.reports = true;
});
afterEach(resetTerminalEndingsForTests);

describe("outcomeFromCloseReason", () => {
  it.each([
    ["terminal session ended; exit=status:0", { kind: "clean" }],
    ["terminal session ended; exit=status:3", { kind: "exited", code: 3 }],
    ["terminal session ended; exit=signal:SIGKILL", { kind: "signaled", signal: "SIGKILL" }],
    ["terminal session ended; exit=status:999", { kind: "unknown" }],
    ["terminal session ended; exit=signal:kill me", { kind: "unknown" }],
  ])("reads %j", (reason, outcome) => {
    expect(outcomeFromCloseReason(reason)).toEqual(outcome);
  });

  it.each(["terminal session ended", "not found", "", "code 4404"])(
    "reads nothing from %j, which names no exit",
    (reason) => {
      expect(outcomeFromCloseReason(reason)).toBeNull();
    },
  );
});

describe("outcomeFromExitPayload", () => {
  it.each([
    [{ status: 0, signal: null }, { kind: "clean" }],
    [
      { status: 3, signal: null },
      { kind: "exited", code: 3 },
    ],
    [
      { status: null, signal: "SIGTERM" },
      { kind: "signaled", signal: "SIGTERM" },
    ],
    [{ status: null, signal: null }, { kind: "unknown" }],
    [{ status: 0 }, { kind: "unknown" }],
    [{ status: 300, signal: null }, { kind: "unknown" }],
    [{ status: 0, signal: "nine" }, { kind: "unknown" }],
    [null, { kind: "unknown" }],
    [[0], { kind: "unknown" }],
  ])("reads %j", (exit, outcome) => {
    expect(outcomeFromExitPayload(exit)).toEqual(outcome);
  });

  it("reads nothing from an event without exit, as an older runner sends", () => {
    expect(outcomeFromExitPayload(undefined)).toBeNull();
  });
});

describe("the endings store", () => {
  it("records a deletion's outcome with the shell's row", () => {
    noteTerminalDeleted("conv", ROW.id, { status: 3, signal: null }, ROW);
    expect(readTerminalEnding("conv", ROW.id)).toEqual({
      outcome: { kind: "exited", code: 3 },
      row: ROW,
    });
  });

  it("keeps the first definitive outcome, and takes a row a later report brings", () => {
    noteTerminalAttachClosed("conv", ROW.id, "terminal session ended; exit=signal:SIGKILL", null);
    noteTerminalDeleted("conv", ROW.id, { status: 0, signal: null }, ROW);
    expect(readTerminalEnding("conv", ROW.id)).toEqual({
      outcome: { kind: "signaled", signal: "SIGKILL" },
      row: ROW,
    });
  });

  it("lets a definitive outcome replace an unknown one, never the other way", () => {
    recordTerminalEnding("conv", ROW.id, { kind: "unknown" }, ROW);
    recordTerminalEnding("conv", ROW.id, { kind: "clean" }, null);
    expect(readTerminalEnding("conv", ROW.id)?.outcome).toEqual({ kind: "clean" });
    recordTerminalEnding("conv", ROW.id, { kind: "unknown" }, null);
    expect(readTerminalEnding("conv", ROW.id)).toEqual({ outcome: { kind: "clean" }, row: ROW });
  });

  it("records nothing for a plain reason or an event without exit", () => {
    noteTerminalAttachClosed("conv", ROW.id, "terminal session ended", ROW);
    noteTerminalDeleted("conv", ROW.id, undefined, ROW);
    expect(readTerminalEnding("conv", ROW.id)).toBeUndefined();
  });

  it("records an exit that arrives before the server's capabilities have loaded", () => {
    server.reports = undefined;
    noteTerminalDeleted("conv", ROW.id, { status: 3, signal: null }, ROW);
    expect(readTerminalEnding("conv", ROW.id)?.outcome).toEqual({ kind: "exited", code: 3 });
    expect(terminalEndingsReported()).toBe(false);
  });

  it("updates its hooks as shells end and are forgotten", () => {
    const one = renderHook(() => useTerminalEnding("conv", ROW.id));
    const all = renderHook(() => useSessionTerminalEndings("conv"));
    const before = all.result.current;
    act(() => noteTerminalDeleted("conv", ROW.id, { status: 0, signal: null }, ROW));
    expect(one.result.current?.outcome).toEqual({ kind: "clean" });
    expect([...all.result.current.keys()]).toEqual([ROW.id]);
    expect(before.size).toBe(0);
    act(() => forgetTerminalEnding("conv", ROW.id));
    expect(one.result.current).toBeUndefined();
    expect(all.result.current.size).toBe(0);
  });
});
