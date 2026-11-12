import { describe, expect, it } from "vitest";

import { describeWorkflowRun, type WorkflowRun } from "./workflowRuns";

function run(
  progress: Partial<WorkflowRun["progress"]>,
  sessionStatus: WorkflowRun["sessionStatus"] = "idle",
): WorkflowRun {
  return {
    id: "c".repeat(32),
    sessionId: "a".repeat(32),
    title: "Fix the upload test",
    startedAt: 1_762_900_000,
    sessionStatus,
    archived: false,
    progress: { total: 5, started: 0, done: 0, needsInput: 0, failed: 0, ...progress },
  };
}

describe("describeWorkflowRun", () => {
  it("puts a question for the user first", () => {
    expect(describeWorkflowRun(run({ done: 2, needsInput: 1 }, "running"))).toEqual({
      kind: "waiting",
      label: "Waiting for you",
    });
  });

  it("says a run whose steps are all done is done", () => {
    expect(describeWorkflowRun(run({ done: 5 }))).toEqual({ kind: "done", label: "Done" });
  });

  it("shows a running run's progress, even past a failed step it may retry", () => {
    expect(describeWorkflowRun(run({ done: 2, failed: 1 }, "running"))).toEqual({
      kind: "running",
      label: "2 of 5 done",
    });
  });

  it("says a stopped run failed, or how far it got", () => {
    expect(describeWorkflowRun(run({ done: 2, failed: 1 }))).toEqual({
      kind: "failed",
      label: "Failed · 2 of 5 done",
    });
    expect(describeWorkflowRun(run({ done: 1 }, "failed")).label).toBe(
      "Session failed · 1 of 5 done",
    );
    expect(describeWorkflowRun(run({ done: 3 }))).toEqual({
      kind: "stopped",
      label: "3 of 5 done",
    });
  });
});
