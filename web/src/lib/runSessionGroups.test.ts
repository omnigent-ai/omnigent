import { describe, expect, it } from "vitest";
import { groupRunsBySession, type RunListEntry } from "@/lib/runSessionGroups";
import type { ScheduledTaskRun } from "@/lib/scheduledTasksApi";

function run(id: string, conversationId: string | null): ScheduledTaskRun {
  return {
    sessionCostUsd: null,
    id,
    scheduledTaskId: "st_1",
    status: "succeeded",
    scheduledAt: 1_700_000_000,
    conversationId,
    firedAt: 1_700_000_000,
    finishedAt: 1_700_000_001,
    errorCode: null,
    errorDetail: null,
    conversationUpdatedAt: null,
    conversationStatus: null,
    viewerLastSeen: null,
    viewerUnread: null,
  };
}

/** Each entry as its run ID, or a session ID with its runs' IDs. */
function shape(entries: RunListEntry[]) {
  return entries.map((entry) =>
    entry.kind === "run"
      ? entry.run.id
      : { session: entry.sessionId, runs: entry.runs.map((item) => item.id) },
  );
}

describe("groupRunsBySession", () => {
  it("lists runs with a session of their own, or none, one by one", () => {
    const runs = [run("r3", "c_3"), run("r2", null), run("r1", "c_1")];
    expect(shape(groupRunsBySession(runs, null))).toEqual(["r3", "r2", "r1"]);
  });

  it("puts runs sharing a session under it, where its newest run was", () => {
    const runs = [
      run("r4", "c_own"),
      run("r3", "c_shared"),
      run("r2", null),
      run("r1", "c_shared"),
    ];
    expect(shape(groupRunsBySession(runs, null))).toEqual([
      "r4",
      { session: "c_shared", runs: ["r3", "r1"] },
      "r2",
    ]);
  });

  it("groups the continued session even when only one run is listed", () => {
    // Its total can include spend from before the automation's first run.
    const runs = [run("r2", "c_continued"), run("r1", "c_earlier")];
    expect(shape(groupRunsBySession(runs, "c_continued"))).toEqual([
      { session: "c_continued", runs: ["r2"] },
      "r1",
    ]);
  });
});
