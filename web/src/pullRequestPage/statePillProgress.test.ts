import { describe, expect, it } from "vitest";

import type { LifecycleNotice } from "./pullRequestLifecycleActions";
import type { StateChainNotice } from "./pullRequestStateChain";
import { statePillProgress } from "./statePillProgress";

const IDLE = {
  stateRunning: null,
  lifecycleRunning: null,
  stateNotice: null,
  lifecycleNotice: null,
};
const stateNotice = (kind: string, operation = "pull_request_close") =>
  ({
    operation,
    outcome: { kind, message: "No.", commentPosted: false },
  }) as unknown as StateChainNotice;
const lifecycleNotice = (kind: string, id = "convert_to_draft") =>
  ({ id, outcome: { kind, code: "x", message: "No." } }) as unknown as LifecycleNotice;

describe("the state pill's progress", () => {
  it("says what's on its way to GitHub", () => {
    expect(statePillProgress({ ...IDLE, stateRunning: "pull_request_close" })).toEqual({
      kind: "running",
      label: "Closing…",
    });
    expect(statePillProgress({ ...IDLE, stateRunning: "pull_request_reopen" })).toEqual({
      kind: "running",
      label: "Reopening…",
    });
    expect(statePillProgress({ ...IDLE, lifecycleRunning: "convert_to_draft" })).toEqual({
      kind: "running",
      label: "Converting to draft…",
    });
    expect(statePillProgress({ ...IDLE, lifecycleRunning: "ready_for_review" })).toEqual({
      kind: "running",
      label: "Marking ready for review…",
    });
  });

  it("says a change failed when GitHub refused it, may not have made it, or it was dropped", () => {
    for (const kind of ["refused", "unknown", "abandoned"]) {
      expect(statePillProgress({ ...IDLE, stateNotice: stateNotice(kind) })).toEqual({
        kind: "failed",
        label: "Couldn’t close the pull request",
      });
      expect(
        statePillProgress({ ...IDLE, lifecycleNotice: lifecycleNotice(kind, "ready_for_review") }),
      ).toEqual({ kind: "failed", label: "Couldn’t mark ready for review" });
    }
    expect(
      statePillProgress({ ...IDLE, stateNotice: stateNotice("refused", "pull_request_reopen") }),
    ).toEqual({ kind: "failed", label: "Couldn’t reopen the pull request" });
  });

  it("shows a running change over an earlier failure", () => {
    expect(
      statePillProgress({
        ...IDLE,
        stateRunning: "pull_request_close",
        lifecycleNotice: lifecycleNotice("refused"),
      }),
    ).toEqual({ kind: "running", label: "Closing…" });
  });

  it("says nothing of auto-merge, a skipped change or a checked one", () => {
    expect(statePillProgress({ ...IDLE, lifecycleRunning: "enable_auto_merge" })).toEqual({
      kind: "idle",
    });
    expect(
      statePillProgress({
        ...IDLE,
        lifecycleNotice: lifecycleNotice("refused", "enable_auto_merge"),
      }),
    ).toEqual({ kind: "idle" });
    expect(statePillProgress({ ...IDLE, stateNotice: stateNotice("skipped") })).toEqual({
      kind: "idle",
    });
    expect(statePillProgress({ ...IDLE, lifecycleNotice: lifecycleNotice("skipped") })).toEqual({
      kind: "idle",
    });
    expect(statePillProgress(IDLE)).toEqual({ kind: "idle" });
  });
});
