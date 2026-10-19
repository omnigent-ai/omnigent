import { beforeEach, describe, expect, it, vi } from "vitest";
import { CODE_RUN_OUTPUT_MAX_CHARS } from "@/lib/codeRuns";
import { blockRuns, codeRunState, useCodeRunStore } from "./codeRunStore";

const SESSION = "conv_a";

function startedItem(runId: string, startedAt = 100, blockHash = "hash1") {
  return {
    id: `item_${runId}_s`,
    type: "code_run",
    run_id: runId,
    phase: "started",
    message_id: "msg_1",
    block_hash: blockHash,
    language: "shell",
    command: "ls",
    timeout: 120,
    started_at: startedAt,
  };
}

function finishedItem(runId: string, text = "done\n") {
  return {
    id: `item_${runId}_f`,
    type: "code_run",
    run_id: runId,
    phase: "finished",
    status: "exited",
    exit_code: 0,
    duration_ms: 1500,
    output: [{ stream: "stdout", text }],
    truncated: true,
  };
}

const store = () => useCodeRunStore.getState();
const run = (runId: string) => store().sessions[SESSION]!.runs[runId]!;

beforeEach(() => {
  useCodeRunStore.setState({ sessions: {} });
});

describe("codeRunStore", () => {
  it("hydrates saved runs and ignores other item types", () => {
    store().ingestItems(SESSION, [
      { id: "msg_1", type: "message", role: "assistant" },
      startedItem("run_1"),
      finishedItem("run_1"),
    ]);
    expect(run("run_1")).toMatchObject({
      messageId: "msg_1",
      blockHash: "hash1",
      language: "shell",
      command: "ls",
      startedAt: 100,
      output: [{ stream: "stdout", text: "done\n" }],
      finished: { status: "exited", exitCode: 0, durationMs: 1500, truncated: true },
    });
  });

  it("appends live output and drops batches it already has by offset", () => {
    store().started(SESSION, startedItem("run_1"));
    store().output(SESSION, "run_1", 0, "stdout", "hello ");
    store().output(SESSION, "run_1", 0, "stdout", "hello ");
    store().output(SESSION, "run_1", 3, "stdout", "lo world");
    store().output(SESSION, "run_1", 11, "stderr", "oops");
    expect(run("run_1").output).toEqual([
      { stream: "stdout", text: "hello world" },
      { stream: "stderr", text: "oops" },
    ]);
    expect(run("run_1").received).toBe(15);
  });

  it("coalesces queued output into one store update per animation frame", () => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame"] });
    try {
      store().started(SESSION, startedItem("run_1"));
      let updates = 0;
      const unsubscribe = useCodeRunStore.subscribe(() => updates++);
      for (let index = 0; index < 10; index++) {
        store().queueOutput(SESSION, "run_1", index, "stdout", "x");
      }
      expect(updates).toBe(0);
      vi.advanceTimersToNextFrame();
      unsubscribe();
      expect(updates).toBe(1);
      expect(run("run_1").output).toEqual([{ stream: "stdout", text: "x".repeat(10) }]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops keeping live output at the saved-output cap and notes the cut", () => {
    store().started(SESSION, startedItem("run_1"));
    const chunk = "y".repeat(30_000);
    for (let index = 0; index < 4; index++) {
      store().output(SESSION, "run_1", index * chunk.length, "stdout", chunk);
    }
    expect(run("run_1").output[0]!.text).toHaveLength(CODE_RUN_OUTPUT_MAX_CHARS);
    expect(run("run_1")).toMatchObject({ received: 120_000, liveCut: true });
    const before = run("run_1");
    store().output(SESSION, "run_1", 0, "stdout", chunk);
    expect(run("run_1")).toBe(before);
  });

  it("takes a snapshot only when it is ahead of what it holds", () => {
    store().started(SESSION, startedItem("run_1"));
    store().output(SESSION, "run_1", 0, "stdout", "abc");
    store().snapshot(SESSION, "run_1", 2, [{ stream: "stdout", text: "ab" }]);
    expect(run("run_1").output).toEqual([{ stream: "stdout", text: "abc" }]);
    store().snapshot(SESSION, "run_1", 10, [{ stream: "stdout", text: "abcdefghij" }]);
    store().output(SESSION, "run_1", 8, "stdout", "ijk");
    expect(run("run_1").output).toEqual([{ stream: "stdout", text: "abcdefghijk" }]);
  });

  it("lets the finished item replace live output", () => {
    store().started(SESSION, startedItem("run_1"));
    store().output(SESSION, "run_1", 0, "stdout", "partial");
    store().finished(SESSION, finishedItem("run_1", "full\n"));
    store().output(SESSION, "run_1", 7, "stdout", "late");
    expect(run("run_1").output).toEqual([{ stream: "stdout", text: "full\n" }]);
  });

  it("keeps live state when a reload re-ingests the started item", () => {
    store().started(SESSION, startedItem("run_1"));
    store().output(SESSION, "run_1", 0, "stdout", "abc");
    store().ingestItems(SESSION, [startedItem("run_1")]);
    expect(run("run_1").output).toEqual([{ stream: "stdout", text: "abc" }]);
    expect(codeRunState(store().sessions[SESSION]!, run("run_1"), true)).toBe("running");
  });

  it("derives running, interrupted, and waiting for an unfinished saved run", () => {
    store().ingestItems(SESSION, [startedItem("run_1"), startedItem("run_2", 101)]);
    store().streamOpened(SESSION);
    store().snapshot(SESSION, "run_2", 0, []);
    const before = store().sessions[SESSION]!;
    expect(codeRunState(before, run("run_1"), true)).toBe("running");
    store().listingSettled(SESSION);
    const after = store().sessions[SESSION]!;
    expect(codeRunState(after, run("run_1"), true)).toBe("interrupted");
    expect(codeRunState(after, run("run_2"), true)).toBe("running");
    expect(codeRunState(after, run("run_1"), false)).toBe("waiting");
    expect(codeRunState(after, run("run_2"), false)).toBe("waiting");
  });

  it("forgets liveness when the stream reconnects", () => {
    store().started(SESSION, startedItem("run_1"));
    store().listingSettled(SESSION);
    store().streamOpened(SESSION);
    store().listingSettled(SESSION);
    expect(codeRunState(store().sessions[SESSION]!, run("run_1"), true)).toBe("interrupted");
  });

  it("lists a block's runs newest first", () => {
    store().ingestItems(SESSION, [
      startedItem("run_old", 100),
      startedItem("run_new", 200),
      startedItem("run_other", 300, "hash2"),
    ]);
    const ids = blockRuns(store().sessions[SESSION], "msg_1", "hash1").map((r) => r.runId);
    expect(ids).toEqual(["run_new", "run_old"]);
    expect(blockRuns(undefined, "msg_1", "hash1")).toEqual([]);
  });

  it("shows a just-accepted run before its started event lands", () => {
    store().accepted(SESSION, "run_1", {
      messageId: "msg_1",
      blockHash: "hash1",
      language: "python",
      command: "print(1)",
    });
    const [latest] = blockRuns(store().sessions[SESSION], "msg_1", "hash1");
    expect(latest).toMatchObject({ runId: "run_1", language: "python" });
    expect(codeRunState(store().sessions[SESSION]!, latest!, true)).toBe("running");
  });

  it("keeps refusals and the copy-on-write flag local to the session", () => {
    store().setRefusal(SESSION, "msg_1:hash1", "Denied by policy");
    store().setCopyOnWrite(SESSION);
    expect(store().sessions[SESSION]).toMatchObject({
      refusals: { "msg_1:hash1": "Denied by policy" },
      copyOnWrite: true,
    });
    store().setRefusal(SESSION, "msg_1:hash1", null);
    expect(store().sessions[SESSION]!.refusals).toEqual({});
  });
});
