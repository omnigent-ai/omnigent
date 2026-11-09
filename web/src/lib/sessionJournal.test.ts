import { describe, expect, it } from "vitest";
import { journalEntry, journalPlan as plan } from "@/test/journalFixtures";
import {
  currentPlan,
  finishedTaskCount,
  isRestart,
  mergeJournalEntries,
  nativeTaskProgress,
  pausedLabel,
  planName,
  planSteps,
  planTally,
  sessionNotes,
  sessionTaskList,
  stepProgress,
  stepsWithoutPlan,
  stepStatusLabel,
  taskPreviewText,
  type JournalEntry,
  type NativeTask,
} from "./sessionJournal";

const RUN = "0b6c5f0d8a7e4c3b9a1d2e3f4a5b6c7d";

function status(position: number, step: string, value: string, fields: Partial<JournalEntry> = {}) {
  return journalEntry(position, {
    kind: "status",
    step,
    payload: { status: value, text: `${step} ${value}` },
    ...fields,
  });
}

describe("mergeJournalEntries", () => {
  it("orders by position and keeps one copy of each entry", () => {
    const merged = mergeJournalEntries(
      [journalEntry(2), journalEntry(0)],
      [journalEntry(1), journalEntry(2, { payload: { body: "newer copy" } })],
    );
    expect(merged.map((entry) => entry.position)).toEqual([0, 1, 2]);
    expect(merged[2].payload.body).toBe("newer copy");
  });
});

describe("stepStatusLabel", () => {
  const now = (1_762_534_860 + 3 * 3600) * 1000;

  it("shows a started step running only while the session works", () => {
    expect(stepStatusLabel("started", 1_762_534_860, true, now)).toBe("Running");
    expect(stepStatusLabel("started", 1_762_534_860, false, now)).toBe("Started 3h ago");
    expect(stepStatusLabel("started", now / 1000 - 5, false, now)).toBe("Started just now");
  });

  it("labels finished statuses whatever the session does", () => {
    expect(stepStatusLabel("done", 0, true, now)).toBe("Done");
    expect(stepStatusLabel("blocked", 0, false, now)).toBe("Blocked");
    expect(stepStatusLabel("failed", 0, false, now)).toBe("Failed");
  });
});

describe("currentPlan", () => {
  it("is the newest plan among the entries", () => {
    expect(currentPlan([plan(0, ["a"]), journalEntry(1), plan(2, ["a"], ["b"])])?.position).toBe(2);
    expect(currentPlan([journalEntry(0)])).toBeNull();
  });
});

describe("stepProgress", () => {
  const steps = planSteps(plan(0, ["a"], ["b"], ["c"], ["d"]));
  const entries = [
    status(1, "a", "started"),
    status(2, "a", "done"),
    status(3, "b", "started"),
    journalEntry(4, { step: "b", payload: { body: "first note" } }),
    journalEntry(5, { step: "b", payload: { body: "second note" } }),
    status(6, "c", "started"),
    status(7, "c", "failed"),
    status(8, "c", "done"),
  ];

  it("gives each step its newest status, its attempt's start and its newest note", () => {
    const idle = stepProgress(steps, entries, false);
    // d has no dependencies, so it can start.
    expect(idle.map((item) => item.state)).toEqual(["done", "stopped", "done", "ready"]);
    expect(idle[0].startedAt).toBe(entries[0].created_at);
    expect(idle[1].note?.payload.body).toBe("second note");
    // c's newest attempt wrote no start (an earlier one failed), so it has no duration.
    expect(idle[2].startedAt).toBeNull();
    expect(idle[3].status).toBeNull();
    expect(stepProgress(steps, entries, true)[1].state).toBe("running");
  });

  it("shows only the current attempt's note once a step starts again", () => {
    const one = planSteps(plan(0, ["a"]));
    const note = (position: number, body: string) =>
      journalEntry(position, { step: "a", payload: { body } });
    const firstTry = [note(1, "Deploying the swap"), status(2, "a", "started")];
    // Before a second start, a note written just ahead of the first one counts.
    expect(stepProgress(one, firstTry, true)[0].note?.payload.body).toBe("Deploying the swap");
    const stopped = [...firstTry, status(3, "a", "failed"), note(4, "Stopped on request")];
    expect(stepProgress(one, stopped, false)[0].note?.payload.body).toBe("Stopped on request");
    const again = [...stopped, status(5, "a", "started")];
    expect(stepProgress(one, again, true)[0].note).toBeNull();
    const later = [...again, note(6, "Redeploying")];
    expect(stepProgress(one, later, true)[0].note?.payload.body).toBe("Redeploying");
  });

  it("marks a start after an earlier one as a restart", () => {
    const first = status(1, "a", "started");
    const failed = status(2, "a", "failed");
    expect(isRestart(first, [])).toBe(false);
    expect(isRestart(failed, [first])).toBe(false);
    expect(isRestart(status(3, "a", "started"), [first, failed])).toBe(true);
    expect(isRestart(status(4, "b", "started"), [first, failed])).toBe(false);
    // Starting again after waiting for the user, a block or a stop resumes the attempt.
    expect(isRestart(status(3, "a", "started"), [first, status(2, "a", "needs_input")])).toBe(
      false,
    );
    expect(isRestart(status(3, "a", "started"), [first, status(2, "a", "blocked")])).toBe(false);
    expect(isRestart(status(3, "a", "started"), [first])).toBe(false);
    expect(isRestart(status(3, "a", "started"), [first, status(2, "a", "done")])).toBe(true);
  });

  it("waits for the user on needs_input, or while a prompt waits on a running task", () => {
    const chain = planSteps(plan(0, ["approve"], ["ship", ["approve"]]));
    const asked = stepProgress(chain, [status(1, "approve", "needs_input")], true);
    // Waiting isn't finished, and what depends on it keeps waiting.
    expect(asked.map((item) => item.state)).toEqual(["needs_input", "waiting"]);
    expect(finishedTaskCount(asked)).toBe(0);
    const started = [status(1, "approve", "started")];
    expect(stepProgress(chain, started, true)[0].state).toBe("running");
    expect(stepProgress(chain, started, true, true)[0].state).toBe("needs_input");
    expect(nativeTaskProgress(TODOS, true, true).map((item) => item.state)).toEqual([
      "done",
      "needs_input",
      "waiting",
    ]);
  });

  it("counts the steps in each state", () => {
    expect(planTally(stepProgress(steps, entries, false))).toEqual({
      waiting: 0,
      ready: 1,
      running: 0,
      stopped: 1,
      needs_input: 0,
      done: 2,
      blocked: 0,
      failed: 0,
    });
  });

  it("reads a plan's name, and none when the entry has no name", () => {
    expect(planName(plan(0, ["a"]))).toBe("The plan");
    expect(planName({ ...plan(0, ["a"]), payload: { steps: [] } })).toBeNull();
    expect(planName(null)).toBeNull();
  });

  it("counts failed steps as finished, and blocked or stopped ones as not", () => {
    const written = [
      status(1, "a", "done"),
      status(2, "b", "failed"),
      status(3, "c", "blocked"),
      status(4, "d", "started"),
    ];
    const progress = stepProgress(planSteps(plan(0, ["a"], ["b"], ["c"], ["d"])), written, false);
    expect(finishedTaskCount(progress)).toBe(2);
  });

  it("makes a step ready once every step it depends on is done", () => {
    const chain = planSteps(plan(0, ["a"], ["b", ["a"]], ["c", ["a", "b"]]));
    const states = (written: JournalEntry[]) =>
      stepProgress(chain, written, false).map((item) => item.state);
    expect(states([])).toEqual(["ready", "waiting", "waiting"]);
    expect(states([status(1, "a", "done")])).toEqual(["done", "ready", "waiting"]);
    expect(states([status(1, "a", "done"), status(2, "b", "failed")])).toEqual([
      "done",
      "failed",
      "waiting",
    ]);
  });

  it("lists the steps the agent named in statuses when there is no plan", () => {
    const named = stepsWithoutPlan([
      status(0, "a", "started"),
      status(1, "a", "done"),
      status(2, "b", "blocked"),
    ]);
    expect(named.map((step) => [step.id, step.name])).toEqual([
      ["a", "a"],
      ["b", "b"],
    ]);
  });
});

describe("pausedLabel", () => {
  const chain = plan(0, ["a"], ["b", ["a"]]);
  const label = (entries: JournalEntry[], working = false) =>
    pausedLabel(stepProgress(planSteps(chain), entries, working), entries, working);

  it("says an idle list stopped while the agent could run a task and none waits for you", () => {
    const entries = [chain, status(1, "a", "done")];
    expect(label(entries)).toBe("Stopped");
    expect(label(entries, true)).toBeNull();
    expect(label([chain, status(1, "a", "done", { scheduled_task_run_id: RUN })])).toBe(
      "Run ended",
    );
  });

  it("says nothing once only waits for the user or blocks are left", () => {
    expect(label([chain, status(1, "a", "done"), status(2, "b", "needs_input")])).toBeNull();
    expect(label([chain, status(1, "a", "done"), status(2, "b", "blocked")])).toBeNull();
    expect(label([chain, status(1, "a", "done"), status(2, "b", "done")])).toBeNull();
  });
});

describe("sessionNotes", () => {
  it("keeps notes that aren't about one step, newest first", () => {
    const notes = sessionNotes([
      journalEntry(0),
      journalEntry(1, { step: "a" }),
      journalEntry(2, { scheduled_task_run_id: RUN }),
      journalEntry(3),
    ]);
    expect(notes.map((entry) => entry.position)).toEqual([3, 2, 0]);
  });
});

const TODOS: NativeTask[] = [
  { content: "Add the flag", status: "completed", activeForm: "Adding the flag" },
  { content: "Document it", status: "in_progress", activeForm: "Documenting it" },
  { content: "Run the tests", status: "pending", activeForm: "Running the tests" },
];

describe("nativeTaskProgress", () => {
  it("maps the agent's own list to tasks with no dependencies or history", () => {
    const progress = nativeTaskProgress(TODOS, true);
    expect(progress.map((item) => [item.step.name, item.state, item.activeForm])).toEqual([
      ["Add the flag", "done", null],
      ["Document it", "running", "Documenting it"],
      ["Run the tests", "waiting", null],
    ]);
    expect(progress.every((item) => item.native && item.step.depends_on.length === 0)).toBe(true);
  });

  it("stops an item in progress while the session is idle, and never marks one ready", () => {
    expect(nativeTaskProgress(TODOS, false).map((item) => item.state)).toEqual([
      "done",
      "stopped",
      "waiting",
    ]);
  });
});

describe("sessionTaskList", () => {
  it("uses the agent's own list while the session has no plan", () => {
    const list = sessionTaskList([journalEntry(0)], TODOS, true);
    expect(list.native).toBe(true);
    expect(list.plan).toBeNull();
    expect(list.progress).toHaveLength(3);
  });

  it("uses the plan, or statuses written without one, over the agent's own list", () => {
    const withPlan = sessionTaskList([plan(0, ["profile"])], TODOS, true);
    expect(withPlan.native).toBe(false);
    expect(withPlan.progress.map((item) => item.step.id)).toEqual(["profile"]);

    const statusesOnly = sessionTaskList([status(0, "objective", "started")], TODOS, true);
    expect(statusesOnly.native).toBe(false);
    expect(statusesOnly.progress.map((item) => item.step.id)).toEqual(["objective"]);
  });
});

describe("taskPreviewText", () => {
  it("names the task in progress, else the next one, else how the list ended", () => {
    expect(taskPreviewText([])).toBeNull();
    expect(taskPreviewText(nativeTaskProgress(TODOS, true))).toBe("Documenting it");
    const next = stepProgress(
      planSteps(plan(0, ["profile"], ["ship", ["profile"]])),
      [status(1, "profile", "done")],
      false,
    );
    expect(taskPreviewText(next)).toBe("Next: ship");
    expect(taskPreviewText(next, "Stopped")).toBe("Stopped · Next: ship");
    // A task waiting for the user comes first, even while another one runs.
    const asking = stepProgress(
      planSteps(plan(0, ["deploy"], ["approve"])),
      [status(1, "deploy", "started"), status(2, "approve", "needs_input")],
      true,
    );
    expect(taskPreviewText(asking)).toBe("Waiting for you: approve");
    const blocked = stepProgress(
      planSteps(plan(0, ["ship"])),
      [status(1, "ship", "blocked")],
      false,
    );
    expect(taskPreviewText(blocked)).toBe("Blocked: ship");
    // A task left started while the session is idle says so.
    const stopped = stepProgress(
      planSteps(plan(0, ["deploy"])),
      [status(1, "deploy", "started")],
      false,
    );
    expect(taskPreviewText(stopped)).toBe("Stopped: deploy");
    expect(taskPreviewText(nativeTaskProgress(TODOS, false))).toBe("Stopped: Document it");
    const done = nativeTaskProgress([{ ...TODOS[0] }], false);
    expect(taskPreviewText(done)).toBe("All done");
    const failed = stepProgress(planSteps(plan(0, ["a"])), [status(1, "a", "failed")], false);
    expect(taskPreviewText(failed)).toBe("Finished, 1 failed");
  });
});
