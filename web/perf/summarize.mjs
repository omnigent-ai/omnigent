// Summarize raw benchmark scenarios and optional component attribution.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { percentile } from "./ab.mjs";

const args = process.argv.slice(2);
const argOf = (name, fb) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fb;
};
const files = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--files") {
    // collect until next --flag
    let j = i + 1;
    while (j < args.length && !args[j].startsWith("--")) {
      files.push(args[j]);
      j++;
    }
    i = j - 1;
  }
}
const outDir = path.join(path.dirname(fileURLToPath(import.meta.url)), "out");
const OUT = path.resolve(argOf("--out", path.join(outDir, "summary.json")));
const inputs = files.length ? files : [path.join(outDir, "results-v2.json")];

const merged = { scenarios: {}, meta: [] };
for (const f of inputs) {
  const data = JSON.parse(fs.readFileSync(f, "utf8"));
  if (merged.meta.length && data.cpuThrottle !== merged.meta[0].throttle)
    throw new Error("cannot merge different throttle rates");
  for (const [k, v] of Object.entries(data.scenarios ?? {})) {
    if (Object.hasOwn(merged.scenarios, k)) throw new Error(`duplicate scenario: ${k}`);
    merged.scenarios[k] = v;
  }
  merged.meta.push({
    file: path.basename(path.dirname(f)),
    throttle: data.cpuThrottle,
    errors: data.consoleErrors?.length ?? 0,
  });
}

function pctile(values, p) {
  if (!values.length) return null;
  return Math.round(percentile(values, p) * 100) / 100;
}
function stats(values) {
  const vs = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (!vs.length) return { n: 0 };
  const mean = vs.reduce((a, b) => a + b, 0) / vs.length;
  const sd = Math.sqrt(vs.reduce((a, b) => a + (b - mean) ** 2, 0) / vs.length);
  return {
    n: vs.length,
    min: pctile(vs, 0),
    p50: pctile(vs, 50),
    p75: pctile(vs, 75),
    p90: pctile(vs, 90),
    max: pctile(vs, 100),
    mean: Math.round(mean * 10) / 10,
    sd: Math.round(sd * 10) / 10,
  };
}

const summary = { meta: merged.meta };

const switchFields = {
  clickToMarkerMs: (x) => x.clickToMarkerMs,
  clickToPaintedMs: (x) => x.clickToPaintedMs,
  clickToWorkDoneMs: (x) => x.clickToWorkDoneMs,
  clickToSettledMs: (x) => x.clickToSettledMs,
  busyTotalMs: (x) => x.busyDuringSwitch?.totalMs,
  longTaskCount: (x) => x.busyDuringSwitch?.count,
  lafTotalMs: (x) => x.lafBusy?.totalMs,
  interactionMs: (x) => x.interaction,
  commits: (x) => x.commits,
  commitsToPaint: (x) => x.commitsToPaint,
  commitsPointerToClick: (x) => x.commitsPointerToClick,
  placeholderVisibleMs: (x) => x.placeholder?.visibleMs,
  placeholderMountedMs: (x) => x.placeholder?.mountedMs,
  domNodes: (x) => x.domNodes,
  heapMB: (x) => x.heapMB,
  scriptDurationMs: (x) => x.cdp?.ScriptDuration * 1000,
  layoutDurationMs: (x) => x.cdp?.LayoutDuration * 1000,
  recalcStyleMs: (x) => x.cdp?.RecalcStyleDuration * 1000,
};

for (const scen of [
  "switch",
  "switchEvicting",
  "switchFiles",
  "switchChanges",
  "switchCollapsedFile",
  "switchCollapsedFileEdit",
]) {
  const rows = merged.scenarios[scen];
  if (!rows) continue;
  const table = {};
  for (const [k, f] of Object.entries(switchFields)) table[k] = stats(rows.map(f));
  table.placeholder = {
    mounted: rows.filter((row) => row.placeholder?.mounted).length,
    painted: rows.filter((row) => row.placeholder?.painted).length,
  };
  table.warmByTarget = {};
  for (const t of [...new Set(rows.map((x) => x.target))]) {
    const rs = rows.filter((x) => x.target === t);
    table.warmByTarget[t] = {
      paintedMs: stats(rs.map((x) => x.clickToPaintedMs)),
      workDoneMs: stats(rs.map((x) => x.clickToWorkDoneMs)),
      settledMs: stats(rs.map((x) => x.clickToSettledMs)),
      domNodes: stats(rs.map((x) => x.domNodes)),
      heapMB: stats(rs.map((x) => x.heapMB)),
    };
  }
  summary[scen] = table;
}

if (merged.scenarios.cold) {
  const c = merged.scenarios.cold;
  const fields = {
    markerVisibleMs: (x) => x.markerVisibleMs,
    paintedMs: (x) => x.paintedMs,
    lcpMs: (x) => x.lcpMs,
    dclMs: (x) => x.dclMs,
    busyTotalMs: (x) => x.busy?.totalMs ?? 0,
    longTaskCount: (x) => x.busy?.count ?? 0,
    commits: (x) => x.commits,
    domNodes: (x) => x.domNodes,
    heapMB: (x) => x.heapMB,
  };
  const table = { all: {}, byTarget: {} };
  for (const [k, f] of Object.entries(fields)) table.all[k] = stats(c.map(f));
  for (const t of [...new Set(c.map((x) => x.target))]) {
    table.byTarget[t] = {
      markerVisibleMs: stats(c.filter((x) => x.target === t).map((x) => x.markerVisibleMs)),
      domNodes: stats(c.filter((x) => x.target === t).map((x) => x.domNodes)),
    };
  }
  summary.coldLoad = table;
}

for (const scen of ["idle"]) {
  const s = merged.scenarios[scen];
  if (!s) continue;
  summary[scen] = {
    busyPct: stats(s.map((x) => x.busyPct)),
    taskDurationMs: stats(s.map((x) => x.taskDurationMs)),
    scriptDurationMs: stats(s.map((x) => x.scriptDurationMs)),
    longTaskCount: stats(s.map((x) => x.longTasks ?? 0)),
    commits: stats(s.map((x) => x.commits)),
    heapMB: stats(s.map((x) => x.heapMB)),
    layoutDurationMs: stats(s.map((x) => x.cdp?.LayoutDuration * 1000)),
    recalcStyleMs: stats(s.map((x) => x.cdp?.RecalcStyleDuration * 1000)),
    droppedFrames: stats(s.map((x) => x.droppedFrames ?? 0)),
  };
  summary[scen + "ByTarget"] = {};
  for (const t of [...new Set(s.map((x) => x.target))]) {
    const rs = s.filter((x) => x.target === t);
    summary[scen + "ByTarget"][t] = {
      taskDurationMs: stats(rs.map((x) => x.taskDurationMs)),
      scriptDurationMs: stats(rs.map((x) => x.scriptDurationMs)),
    };
  }
}

if (merged.scenarios.stream) {
  const s = merged.scenarios.stream;
  summary.stream = {
    busyPct: stats(s.map((x) => x.busyPct)),
    taskDurationMs: stats(s.map((x) => x.taskDurationMs)),
    scriptDurationMs: stats(s.map((x) => x.scriptDurationMs)),
    longTaskCount: stats(s.map((x) => x.longTasks)),
    commits: stats(s.map((x) => x.commits)),
    commitsPerEvent: stats(s.map((x) => x.commitsPerEvent)),
    heapMB: stats(s.map((x) => x.heapMB)),
    layoutDurationMs: stats(s.map((x) => x.cdp?.LayoutDuration * 1000)),
    recalcStyleMs: stats(s.map((x) => x.cdp?.RecalcStyleDuration * 1000)),
    droppedFrames: stats(s.map((x) => x.droppedFrames)),
  };
}

if (merged.scenarios.deepHistory) {
  const d = merged.scenarios.deepHistory;
  summary.deepHistory = {
    load: d.load,
    loadStats: stats(d.load.map((sample) => sample.loadMs)),
    idleDeep: d.idleDeep,
    streamStats: Object.fromEntries(
      [
        "taskDurationMs",
        "scriptDurationMs",
        "taskMsPerEvent",
        "busyPct",
        "longTasks",
        "commits",
        "commitsPerEvent",
        "droppedFrames",
      ].map((key) => [key, stats((d.stream ?? []).map((sample) => sample[key]))]),
    ),
    stream: (d.stream ?? []).map((x) => ({
      target: x.target,
      commits: x.commits,
      commitsPerEvent: x.commitsPerEvent,
      taskDurationMs: Math.round(x.taskDurationMs),
      idleTaskMs: Math.round(x.idleTaskMs ?? 0),
      taskMsPerEvent: x.taskMsPerEvent ?? null,
      busyPct: x.busyPct,
      longTasks: x.longTasks,
    })),
  };
}

fs.mkdirSync(path.dirname(OUT), { recursive: true });
fs.writeFileSync(OUT, JSON.stringify(summary, null, 1));
console.log(`summary (${inputs.length} input files) → ${OUT}`);
