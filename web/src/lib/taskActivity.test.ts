import { describe, expect, it } from "vitest";
import { journalEntry, journalPlan } from "@/test/journalFixtures";
import { nativeTaskProgress, planSteps, stepProgress } from "./sessionJournal";
import {
  planChangeText,
  readTaskActivity,
  taskMarkerId,
  taskMarkerLabel,
  taskMarkerTarget,
  todoMarkerId,
  type TaskActivity,
} from "./taskActivity";

function marker(eventType: string, resource: Record<string, unknown>, resourceId = "deploy") {
  return {
    type: "resource_event",
    event_type: eventType,
    resource_type: "journal",
    resource_id: resourceId,
    resource,
  };
}

describe("readTaskActivity", () => {
  it("reads a task's marker and a plan change, and nothing else", () => {
    const done = marker("session.journal.task", {
      name: "Deploy the swap",
      status: "done",
      text: "Pod 1/1",
      entry_id: "e1",
      native: false,
    });
    expect(readTaskActivity(done)).toEqual({
      kind: "task",
      stepId: "deploy",
      name: "Deploy the swap",
      status: "done",
      text: "Pod 1/1",
      entryId: "e1",
      native: false,
    });
    const made = marker(
      "session.journal.plan",
      { change: "made", name: "Ship it", tasks: 3, added: [], removed: [], entry_id: "e0" },
      "e0",
    );
    expect(readTaskActivity(made)).toMatchObject({ kind: "plan", change: "made", tasks: 3 });
    expect(readTaskActivity(marker("session.journal.task", { status: "running" }))).toBeNull();
    const subagent = {
      type: "resource_event",
      event_type: "session.subagent.delegated",
      resource_type: "session",
      resource_id: "child",
      resource: { title: "Reviewer" },
    };
    expect(readTaskActivity(subagent)).toBeNull();
  });
});

describe("planChangeText", () => {
  const plan = (fields: Partial<Extract<TaskActivity, { kind: "plan" }>>) =>
    ({
      kind: "plan",
      entryId: "e",
      change: "updated",
      name: "Ship it",
      tasks: 4,
      added: [],
      removed: [],
      ...fields,
    }) as Extract<TaskActivity, { kind: "plan" }>;

  it("says in one line how the plan changed", () => {
    expect(planChangeText(plan({ change: "made" }))).toBe("Made a plan: Ship it · 4 tasks");
    expect(planChangeText(plan({ change: "replaced", name: "Fix the test", tasks: 1 }))).toBe(
      "Replaced the plan with Fix the test · 1 task",
    );
    expect(planChangeText(plan({ change: "renamed", name: "Ship it now" }))).toBe(
      "Renamed the plan to Ship it now",
    );
    expect(
      planChangeText(plan({ added: ["Pre-warm the pool"], removed: ["Overlap the check"] })),
    ).toBe("Updated the plan: added Pre-warm the pool, removed Overlap the check");
    // Past a few names it counts them, and a change that adds or removes none says only that.
    expect(planChangeText(plan({ added: ["a", "b", "c", "d", "e", "f"] }))).toBe(
      "Updated the plan: added 6 tasks",
    );
    expect(planChangeText(plan({}))).toBe("Updated the plan");
  });

  it("leads a marker with what happened to the task", () => {
    expect(
      ["started", "done", "failed", "blocked", "needs_input"].map((s) =>
        taskMarkerLabel(s as never),
      ),
    ).toEqual(["Started", "Completed", "Failed", "Blocked", "Waiting for you on"]);
  });
});

describe("taskMarkerTarget", () => {
  it("links a started task to its newest status's marker, and one not started nowhere", () => {
    const steps = planSteps(journalPlan(0, ["deploy"], ["verify", ["deploy"]]));
    const started = journalEntry(1, {
      kind: "status",
      step: "deploy",
      payload: { status: "started", text: null },
    });
    const done = journalEntry(2, {
      kind: "status",
      step: "deploy",
      payload: { status: "done", text: "ok" },
    });
    expect(taskMarkerTarget(stepProgress(steps, [started], true)[0])).toBe("journal-e1");
    const progress = stepProgress(steps, [started, done], false);
    expect(progress.map(taskMarkerTarget)).toEqual(["journal-e2", null]);
    // The marker the server wrote for that entry answers to the same ID.
    expect(
      taskMarkerId({
        kind: "task",
        stepId: "deploy",
        name: "Deploy",
        status: "done",
        text: null,
        entryId: "e2",
        native: false,
      }),
    ).toBe("journal-e2");
  });

  it("links the agent's own tasks by their ID and status", () => {
    const own = nativeTaskProgress(
      [
        { id: "7", content: "Document it", status: "in_progress", activeForm: "Documenting it" },
        { content: "Run the tests", status: "pending", activeForm: "Running the tests" },
        { content: "Add the flag", status: "completed", activeForm: "Adding the flag" },
      ],
      true,
    );
    expect(own.map((item) => item.step.id)).toEqual(["7", "task-2", "task-3"]);
    expect(own.map(taskMarkerTarget)).toEqual([
      todoMarkerId("7", "started"),
      null,
      todoMarkerId("task-3", "done"),
    ]);
  });
});
