import { describe, expect, it } from "vitest";

import { SANDBOX_RESUME_TOTAL_LIMIT_MS } from "@/lib/sandboxResume";

import {
  SHELL_LIST_LIMIT_MS,
  START_TIMED_OUT,
  type TerminalTabInputs,
  WAKE_TIMED_OUT,
  terminalTabConnection,
  terminalTabSpins,
  terminalTabWait,
} from "./terminalTabConnection";

const NONE = { status: "none", ownStatus: "none", failure: null } as const;

function inputs(overrides: Partial<TerminalTabInputs> = {}): TerminalTabInputs {
  return {
    inventory: "unknown",
    listFailed: false,
    liveness: "online",
    hostResumable: false,
    sandbox: NONE,
    waited: 0,
    ...overrides,
  };
}

describe("terminalTabConnection", () => {
  it("is live once the session lists the shell, whatever the session reads", () => {
    for (const liveness of ["online", "runner_asleep", "host_offline", "starting"] as const) {
      expect(terminalTabConnection(inputs({ inventory: "live", liveness }))).toEqual({
        kind: "live",
      });
    }
  });

  it("shows a sleeping, waking or failed sandbox with its failure", () => {
    for (const status of ["asleep", "resuming", "failed"] as const) {
      const failure = status === "failed" ? "No capacity" : null;
      expect(
        terminalTabConnection(inputs({ sandbox: { status, ownStatus: status, failure } })),
      ).toEqual({ kind: "sandbox", status, failure });
    }
  });

  it("reads a launch stage someone else reported as asleep once the wake limit passes", () => {
    const sandbox = { status: "resuming", ownStatus: "none", failure: null } as const;
    const waited = SANDBOX_RESUME_TOTAL_LIMIT_MS;
    expect(terminalTabConnection(inputs({ sandbox, waited: waited - 1 })).kind).toBe("sandbox");
    expect(terminalTabConnection(inputs({ sandbox, waited }))).toEqual({
      kind: "sandbox",
      status: "asleep",
      failure: null,
    });
  });

  it("says the host is offline, the session has no host and stopped, or it isn't running", () => {
    expect(terminalTabConnection(inputs({ liveness: "host_offline" }))).toEqual({
      kind: "host_offline",
    });
    // No host is bound, so there's none to reconnect; whoever started it relaunches it.
    expect(terminalTabConnection(inputs({ liveness: "local_stranded" }))).toEqual({
      kind: "local_stranded",
    });
    expect(terminalTabConnection(inputs({ liveness: "runner_asleep" }))).toEqual({
      kind: "runner_asleep",
      failure: null,
    });
  });

  it("connects while the session starts, then says the start or the wake took too long", () => {
    const starting = (waited: number, hostResumable = false) =>
      terminalTabConnection(inputs({ liveness: "starting", hostResumable, waited }));
    expect(starting(SANDBOX_RESUME_TOTAL_LIMIT_MS - 1)).toEqual({ kind: "connecting" });
    const expired = SANDBOX_RESUME_TOTAL_LIMIT_MS;
    expect(starting(expired)).toEqual({ kind: "runner_asleep", failure: START_TIMED_OUT });
    expect(starting(expired, true)).toEqual({
      kind: "sandbox",
      status: "failed",
      failure: WAKE_TIMED_OUT,
    });
  });

  it("says a shell the list no longer has has ended", () => {
    expect(terminalTabConnection(inputs({ inventory: "gone" }))).toEqual({
      kind: "ended",
    });
  });

  it("connects until the list shows the shell, and gives up when it fails or takes too long", () => {
    expect(terminalTabConnection(inputs())).toEqual({ kind: "connecting" });
    expect(terminalTabConnection(inputs({ listFailed: true }))).toEqual({ kind: "unreachable" });
    const waited = (ms: number) => terminalTabConnection(inputs({ waited: ms }));
    expect(waited(SHELL_LIST_LIMIT_MS - 1)).toEqual({ kind: "connecting" });
    expect(waited(SHELL_LIST_LIMIT_MS)).toEqual({ kind: "unreachable" });
  });
});

describe("terminalTabSpins", () => {
  it("spins while a shell connects or its sandbox wakes, and not otherwise", () => {
    expect(terminalTabSpins({ kind: "connecting" })).toBe(true);
    expect(terminalTabSpins({ kind: "sandbox", status: "resuming", failure: null })).toBe(true);
    expect(terminalTabSpins({ kind: "sandbox", status: "asleep", failure: null })).toBe(false);
    for (const kind of ["live", "ended", "unreachable", "host_offline"] as const) {
      expect(terminalTabSpins({ kind })).toBe(false);
    }
  });
});

describe("terminalTabWait", () => {
  const facts = {
    inventory: "unknown",
    listFailed: false,
    liveness: "online",
    sandbox: NONE,
  } as const;

  it("names what a shell that isn't listed waits for, in the order its connection reads them", () => {
    expect(terminalTabWait(facts)).toBe("shell_list");
    expect(terminalTabWait({ ...facts, liveness: "unknown" })).toBe("shell_list");
    expect(terminalTabWait({ ...facts, liveness: "starting" })).toBe("starting");
    const reported = { status: "resuming", ownStatus: "none", failure: null } as const;
    expect(terminalTabWait({ ...facts, liveness: "starting", sandbox: reported })).toBe(
      "reported_stage",
    );
  });

  it("doesn't wait for a listed or ended shell, a failed list, or a session that can't answer", () => {
    expect(terminalTabWait({ ...facts, inventory: "live" })).toBeNull();
    expect(terminalTabWait({ ...facts, inventory: "gone" })).toBeNull();
    expect(terminalTabWait({ ...facts, listFailed: true })).toBeNull();
    for (const liveness of ["runner_asleep", "host_offline", "local_stranded"] as const) {
      expect(terminalTabWait({ ...facts, liveness })).toBeNull();
    }
    // This app's own resume, and an asleep sandbox, aren't timed here.
    const own = { status: "resuming", ownStatus: "resuming", failure: null } as const;
    const asleep = { status: "asleep", ownStatus: "none", failure: null } as const;
    expect(terminalTabWait({ ...facts, sandbox: own })).toBeNull();
    expect(terminalTabWait({ ...facts, sandbox: asleep })).toBeNull();
  });
});
