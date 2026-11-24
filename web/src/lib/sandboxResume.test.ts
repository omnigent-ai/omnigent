import { describe, expect, it } from "vitest";

import {
  reduceSandboxResume,
  sandboxResumeDisplay,
  SANDBOX_RESUME_QUIET_LIMIT_MS,
  SANDBOX_RESUME_TIMED_OUT,
  SANDBOX_RESUME_TOTAL_LIMIT_MS,
  type SandboxResumeEvent,
  type SandboxResumeState,
} from "./sandboxResume";
import type { SandboxStatus } from "./types";

const timedOut: SandboxStatus = { stage: "failed", error: "Sandbox launch timed out" };
const approve: SandboxStatus = { stage: "action_required", details: "Approve the sandbox" };

function run(
  events: SandboxResumeEvent[],
  initial?: SandboxResumeState,
): SandboxResumeState | undefined {
  return events.reduce(reduceSandboxResume, initial);
}

const start = (launch: SandboxStatus | null = null, now = 0, attempt = 1): SandboxResumeEvent => ({
  type: "start",
  attempt,
  now,
  launch,
});
const answered = (now = 0, attempt = 1): SandboxResumeEvent => ({
  type: "request_succeeded",
  attempt,
  now,
});

describe("reduceSandboxResume", () => {
  it("resumes, waits for the host, and ends once it is online", () => {
    const requesting = run([start()]);
    expect(requesting).toMatchObject({ phase: "requesting", attempt: 1 });
    const awaiting = run([answered()], requesting);
    expect(awaiting).toMatchObject({ phase: "awaiting_online", attempt: 1 });
    expect(run([{ type: "host_online" }], awaiting)).toBeUndefined();
  });

  it("joins a resume that is already running rather than starting another", () => {
    const requesting = run([start()]);
    expect(run([start()], requesting)).toBe(requesting);
    const awaiting = run([answered()], requesting);
    expect(run([start()], awaiting)).toBe(awaiting);
  });

  it("ignores liveness read before the server answered", () => {
    const requesting = run([start()]);
    expect(run([{ type: "host_online" }], requesting)).toBe(requesting);
  });

  it("reports a failed request, then retries as a new attempt", () => {
    const failed = run([start(), { type: "request_failed", attempt: 1, message: "Nope" }]);
    expect(failed).toEqual({ phase: "failed", attempt: 1, message: "Nope" });
    const retry = run([start(null, 0, 2)], failed);
    expect(retry).toMatchObject({ phase: "requesting", attempt: 2 });
    // The first attempt's late answer doesn't move the retry along.
    expect(run([answered(0, 1)], retry)).toBe(retry);
    expect(run([{ type: "request_failed", attempt: 1, message: "late" }], retry)).toBe(retry);
  });

  it("clears a failure once the sandbox comes up anyway", () => {
    const failed: SandboxResumeState = { phase: "failed", attempt: 1, message: "Nope" };
    expect(run([{ type: "host_online" }], failed)).toBeUndefined();
  });

  it("fails on a launch failure reported after the click, with its reason", () => {
    const state = run([
      start(),
      answered(),
      { type: "launch", launch: { stage: "provisioning" }, now: 1 },
      { type: "launch", launch: timedOut, now: 2 },
    ]);
    expect(state).toEqual({
      phase: "failed",
      attempt: 1,
      message: "The sandbox couldn't start: Sandbox launch timed out",
    });
  });

  it.each([
    ["the same failure", timedOut, [timedOut]],
    ["a rebuilt copy of the failure", timedOut, [{ ...timedOut }]],
    ["the same user action", approve, [{ ...approve }]],
  ] as const)("keeps going past %s left from before the click", (_case, before, stages) => {
    const state = run([
      start(before),
      ...stages.map((launch) => ({ type: "launch" as const, launch, now: 1 })),
    ]);
    expect(state).toMatchObject({ phase: "requesting" });
  });

  it("fails on the same failure again once the launch moved on", () => {
    const state = run([
      start(timedOut),
      { type: "launch", launch: { stage: "provisioning" }, now: 1 },
      { type: "launch", launch: timedOut, now: 2 },
    ]);
    expect(state).toMatchObject({ phase: "failed" });
  });

  it("lets the launch banner explain a stage that waits on the user", () => {
    const state = run([start(), { type: "launch", launch: approve, now: 1 }]);
    expect(state).toBeUndefined();
  });

  it("times out when the host stays offline with no launch progress", () => {
    const awaiting = run([start(null, 0), answered(1_000)]);
    expect(run([{ type: "tick", now: 1_000 + SANDBOX_RESUME_QUIET_LIMIT_MS - 1 }], awaiting)).toBe(
      awaiting,
    );
    expect(run([{ type: "tick", now: 1_000 + SANDBOX_RESUME_QUIET_LIMIT_MS }], awaiting)).toEqual({
      phase: "failed",
      attempt: 1,
      message: SANDBOX_RESUME_TIMED_OUT,
    });
  });

  it("holds the quiet limit while a launch stage reports progress, then restarts it", () => {
    const later = SANDBOX_RESUME_QUIET_LIMIT_MS * 1.5;
    const progressing = run([
      start(),
      answered(),
      { type: "launch", launch: { stage: "provisioning" }, now: 10 },
      { type: "tick", now: later },
    ]);
    expect(progressing).toMatchObject({ phase: "awaiting_online" });
    // `ready` clears the stage: the host gets a full quiet window from there.
    const ready = run([{ type: "launch", launch: null, now: later }], progressing);
    expect(run([{ type: "tick", now: later + SANDBOX_RESUME_QUIET_LIMIT_MS - 1 }], ready)).toBe(
      ready,
    );
    expect(
      run([{ type: "tick", now: later + SANDBOX_RESUME_QUIET_LIMIT_MS }], ready),
    ).toMatchObject({ phase: "failed" });
  });

  it("gives up at the total limit even while stages keep changing, answered or not", () => {
    for (const events of [[start()], [start(), answered()]]) {
      const state = run([
        ...events,
        { type: "launch", launch: { stage: "provisioning" }, now: 1 },
        { type: "launch", launch: { stage: "connecting" }, now: SANDBOX_RESUME_TOTAL_LIMIT_MS - 1 },
      ]);
      expect(run([{ type: "tick", now: SANDBOX_RESUME_TOTAL_LIMIT_MS - 1 }], state)).toBe(state);
      expect(run([{ type: "tick", now: SANDBOX_RESUME_TOTAL_LIMIT_MS }], state)).toMatchObject({
        phase: "failed",
        message: SANDBOX_RESUME_TIMED_OUT,
      });
    }
  });

  it("doesn't apply the quiet limit before the server answers", () => {
    const requesting = run([start()]);
    expect(run([{ type: "tick", now: SANDBOX_RESUME_QUIET_LIMIT_MS * 2 }], requesting)).toBe(
      requesting,
    );
  });

  it("ignores an outcome for an attempt whose state was cleared, once a new one starts", () => {
    for (const cleared of [
      { type: "launch", launch: approve, now: 1 } as const,
      { type: "host_online" } as const,
    ]) {
      // A cleared on the user's action (or the host coming up after a failure), then B.
      const first =
        cleared.type === "host_online"
          ? run([start(), { type: "request_failed", attempt: 1, message: "x" }, cleared])
          : run([start(), cleared]);
      expect(first).toBeUndefined();
      const second = run([start(null, 2, 2)], first);
      expect(run([{ type: "request_failed", attempt: 1, message: "late" }], second)).toBe(second);
      expect(run([answered(3, 1)], second)).toBe(second);
    }
  });

  it("does nothing while idle", () => {
    expect(run([{ type: "host_online" }])).toBeUndefined();
    expect(run([{ type: "tick", now: 1 }])).toBeUndefined();
    expect(run([{ type: "launch", launch: timedOut, now: 1 }])).toBeUndefined();
    expect(run([answered()])).toBeUndefined();
  });
});

describe("sandboxResumeDisplay", () => {
  it("keeps a failed resume and its Retry while the host is still starting, and drops it once it's up", () => {
    const failed: SandboxResumeState = { phase: "failed", attempt: 1, message: "Nope" };
    expect(sandboxResumeDisplay(failed, "starting", false)).toEqual({
      status: "failed",
      resumeStatus: "failed",
      failure: "Nope",
    });
    expect(sandboxResumeDisplay(failed, "starting", true).status).toBe("failed");
    expect(sandboxResumeDisplay(failed, "runner_asleep", false).status).toBe("none");
    expect(sandboxResumeDisplay(failed, "online", false).status).toBe("none");
  });

  const failed: SandboxResumeState = { phase: "failed", attempt: 1, message: "It broke." };
  const requesting = run([start()]);

  it("is none for a live sandbox with nothing reported", () => {
    expect(sandboxResumeDisplay(undefined, "online", false)).toEqual({
      status: "none",
      resumeStatus: "none",
      failure: null,
    });
  });

  it("offers Resume for an asleep sandbox", () => {
    expect(sandboxResumeDisplay(undefined, "host_asleep", false).status).toBe("asleep");
  });

  it("shows the session's own resume as resuming wherever its host is", () => {
    expect(sandboxResumeDisplay(requesting, "online", false).status).toBe("resuming");
    expect(sandboxResumeDisplay(requesting, "host_asleep", false).status).toBe("resuming");
  });

  it("shows a reported launch as resuming while the sandbox is asleep or starting, but keeps menus on the session's own resume", () => {
    expect(sandboxResumeDisplay(undefined, "host_asleep", true)).toMatchObject({
      status: "resuming",
      resumeStatus: "asleep",
    });
    expect(sandboxResumeDisplay(undefined, "starting", true)).toMatchObject({
      status: "resuming",
      resumeStatus: "none",
    });
  });

  it("ignores a reported launch once the sandbox reads otherwise", () => {
    expect(sandboxResumeDisplay(undefined, "online", true).status).toBe("none");
    expect(sandboxResumeDisplay(undefined, "runner_asleep", true).status).toBe("none");
  });

  it("keeps the session's own failure and Retry over a reported launch", () => {
    expect(sandboxResumeDisplay(failed, "host_asleep", true)).toEqual({
      status: "failed",
      resumeStatus: "failed",
      failure: "It broke.",
    });
  });

  it("drops a failure once the sandbox is up", () => {
    expect(sandboxResumeDisplay(failed, "online", false)).toEqual({
      status: "none",
      resumeStatus: "none",
      failure: null,
    });
  });
});
