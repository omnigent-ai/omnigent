import assert from "node:assert/strict";
import { test } from "node:test";
import { analyzeTrace } from "./trace-analysis.mjs";

test("phase totals count main-thread tasks once and split across paint", () => {
  const mark = (name, ts) => ({ name, ts, pid: 1, tid: 1, cat: "blink.user_timing" });
  const task = (ts, dur, tid = 1) => ({ name: "RunTask", ts, dur, pid: 1, tid });
  const events = [
    mark("pf:click", 1000),
    mark("pf:painted", 101000),
    mark("pf:settled", 201000),
    task(21000, 100000),
    { name: "FunctionCall", ts: 31000, dur: 50000, pid: 1, tid: 1 },
    task(131000, 50000),
    task(1000, 200000, 2),
  ];
  const [phase] = analyzeTrace(events);
  assert.equal(phase.prePaintMs, 80);
  assert.equal(phase.postPaintMs, 70);
  assert.equal(phase.paintedAfterClickMs, 100);
});
