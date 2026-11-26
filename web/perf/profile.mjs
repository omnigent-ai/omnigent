// Capture a warm-switch timeline, CPU profile and pre/post-paint phases.
// Requires the mock server serving the unminified build (--dist dist-nomin).

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { SourceMap, createRequire } from "node:module";
import { cdpConnect, ensurePortFree, findChrome, forEachSequential, waitHttp } from "./cdp.mjs";
import { analyzeTrace } from "./trace-analysis.mjs";

const require = createRequire(import.meta.url);
const { execFileSync } = require("node:child_process");

const args = process.argv.slice(2);
const argOf = (name, fb) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fb;
};
const BASE = argOf("--url", "http://127.0.0.1:5299");
const OUT = path.resolve(
  argOf("--out", path.join(path.dirname(fileURLToPath(import.meta.url)), "out", "traces")),
);
const CHROME = findChrome(argOf("--chrome"));
const DBG_PORT = Number(argOf("--cdp-port", "9336"));

const MARKERS = {
  conv_perf_a: "SESSMARK-RF_A",
  conv_perf_b: "SESSMARK-RF_B",
  conv_perf_c: "SESSMARK-RF_C",
  conv_perf_d: "SESSMARK-RF_D",
  conv_perf_e: "SESSMARK-RF_E",
};
const TITLES = {
  conv_perf_a: "Perf A — extreme streaming replay",
  conv_perf_b: "Perf B — docs rewrite",
  conv_perf_c: "Perf C — refactor pass",
  conv_perf_d: "Perf D — data pipeline",
  conv_perf_e: "Perf E — migration",
};

fs.mkdirSync(OUT, { recursive: true });

const INIT_SCRIPT = `
(() => {
  window.__perf = { longTasks: [], lafs: [], clickTs: -1, markerTs: {}, dirty: false, wantedMarkers: [] };
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.longTasks.push({ s: e.startTime, d: e.duration }); }).observe({ type: "longtask", buffered: true });
  } catch {}
  try {
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__perf.lafs.push({ s: e.startTime, d: e.duration }); }).observe({ type: "long-animation-frame", buffered: true });
  } catch {}
  window.addEventListener("click", () => { window.__perf.clickTs = performance.now(); try { performance.mark("pf:click"); } catch {} }, { capture: true });
  window.__markerSeen = (m) => (window.__perf.markerTs[m] !== undefined ? window.__perf.markerTs[m] : -1);
  setInterval(() => {
    if (!window.__perf.dirty) return;
    window.__perf.dirty = false;
    const text = document.body?.textContent ?? "";
    for (const m of window.__perf.wantedMarkers ?? []) {
      if (window.__perf.markerTs[m] === undefined && text.includes(m)) {
        window.__perf.markerTs[m] = performance.now();
        try { performance.mark("pf:marker"); } catch {}
      }
    }
  }, 20);
  const mo = new MutationObserver(() => { window.__perf.dirty = true; });
  const arm = () => mo.observe(document.body ?? document.documentElement, { childList: true, subtree: true, characterData: true });
  if (document.body) arm(); else document.addEventListener("DOMContentLoaded", arm, { once: true });
  window.__resetPerf = (marker) => { window.__perf.markerTs = {}; window.__perf.wantedMarkers = [marker]; window.__perf.longTasks = []; window.__perf.lafs = []; window.__perf.dirty = true; };
  window.__waitSettled = async (marker, timeoutMs = 25000, quietMs = 250) => {
    const t0 = performance.now();
    window.__perf.wantedMarkers = [...new Set([...(window.__perf.wantedMarkers ?? []), marker])];
    const raf = () => new Promise((r) => requestAnimationFrame(r));
    let markerTs = -1;
    while (markerTs < 0) {
      if (performance.now() - t0 > timeoutMs) return { markerTs: -1 };
      await new Promise((r) => setTimeout(r, 10));
      markerTs = window.__markerSeen(marker);
    }
    await raf();
    await new Promise((r) => setTimeout(r, 0));
    const paintedTs = performance.now();
    try { performance.mark("pf:painted"); } catch {}
    const busyEndNow = () => {
      let end = -1;
      for (const lt of window.__perf.longTasks) end = Math.max(end, lt.s + lt.d);
      for (const lf of window.__perf.lafs) end = Math.max(end, lf.s + lf.d);
      return end;
    };
    let settleTs = -1;
    while (performance.now() < paintedTs + timeoutMs) {
      await new Promise((r) => setTimeout(r, 25));
      const busyEnd = busyEndNow();
      if (busyEnd < 0 || performance.now() - busyEnd > quietMs) { settleTs = performance.now(); break; }
    }
    try { performance.mark("pf:settled"); } catch {}
    return { markerTs, paintedTs, settleTs };
  };
})();
`;

await ensurePortFree(DBG_PORT);
const PROFILE_DIR = fs.mkdtempSync(path.join(os.tmpdir(), "perf-prof-"));
const child = spawn(
  CHROME,
  [
    `--user-data-dir=${PROFILE_DIR}`,
    `--remote-debugging-port=${DBG_PORT}`,
    "--headless=new",
    "--no-first-run",
    "--disable-features=Translate,BackForwardCache",
    "--window-size=1600,1000",
    "about:blank",
  ],
  { stdio: "ignore" },
);

async function evalNow(cdp, expression, awaitPromise = false) {
  const res = await cdp.send("Runtime.evaluate", { expression, awaitPromise, returnByValue: true });
  if (res.exceptionDetails) throw new Error(`eval failed: ${res.exceptionDetails.text}`);
  return res.result?.value;
}

const smCache = new Map();
function sourcemapFor(url) {
  if (!url || !url.startsWith("http")) return null;
  if (smCache.has(url)) return smCache.get(url);
  let sm = null;
  try {
    const mapUrl = `${url}.map`;
    const dir = path.join(OUT, "_sourcemaps");
    fs.mkdirSync(dir, { recursive: true });
    const name = mapUrl
      .split("/")
      .slice(3)
      .join("__")
      .replace(/[^a-zA-Z0-9._-]/g, "_");
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) {
      fs.writeFileSync(
        file,
        execFileSync("curl", ["-fsS", "--max-time", "15", mapUrl], {
          maxBuffer: 1024 * 1024 * 1024,
        }),
      );
    }
    const payload = JSON.parse(fs.readFileSync(file, "utf8"));
    if (payload && payload.version && payload.sources) sm = new SourceMap(payload);
  } catch {
    sm = null;
  }
  smCache.set(url, sm);
  return sm;
}
function mapOrig(url, line, col) {
  const sm = sourcemapFor(url);
  if (sm && line >= 0) {
    try {
      const e = sm.findEntry(line, col ?? 0);
      if (e && e.originalSource)
        return `${e.originalSource.replace(/^.*?src\//, "src/")}:${e.originalLine + 1}`;
    } catch {
      return null;
    }
  }
  return null;
}

function bottomUp(profile) {
  const { nodes, samples, timeDeltas } = profile;
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const self = new Map();
  let total = 0;
  for (let i = 0; i < samples.length; i++) {
    const node = nodeById.get(samples[i]);
    const dt = Math.max(0, timeDeltas[i] ?? 0);
    total += dt;
    if (!node) continue;
    const cf = node.callFrame;
    const orig = cf.url.includes("/assets/")
      ? mapOrig(cf.url, cf.lineNumber, cf.columnNumber)
      : null;
    const key = `${cf.functionName || "(anon)"}|${orig ?? cf.url.replace(/^.*assets\//, "assets/") + ":" + (cf.lineNumber + 1)}`;
    self.set(key, (self.get(key) ?? 0) + dt);
  }
  const rows = [...self.entries()]
    .map(([k, us]) => {
      const [fn, loc] = k.split("|");
      return {
        function: fn,
        where: loc,
        selfMs: Math.round(us / 100) / 10,
        selfPct: Math.round((us / Math.max(1, total)) * 1000) / 10,
      };
    })
    .sort((a, b) => b.selfMs - a.selfMs)
    .slice(0, 80);
  return { totalMs: Math.round(total / 100) / 10, rows };
}

async function main() {
  const version = await waitHttp(`http://127.0.0.1:${DBG_PORT}/json/version`);
  const cdp = cdpConnect(version.webSocketDebuggerUrl);
  await cdp.ready;
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  cdp.setDefaultSession(sessionId);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Profiler.enable");
  await cdp.send("HeapProfiler.enable");
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: INIT_SCRIPT });
  let traceChunks = [];
  cdp.onEvent((method, params) => {
    if (method === "Tracing.dataCollected" && params.value) {
      for (const e of params.value) traceChunks.push(e);
    }
  });
  async function startTrace() {
    traceChunks = [];
    // FunctionCall events retain source locations without per-event stacks.
    await cdp.send("Tracing.start", {
      traceConfig: {
        includedCategories: ["toplevel", "devtools.timeline", "blink.user_timing"],
      },
    });
    await cdp.send("Profiler.start");
  }
  async function stopTrace(name) {
    const complete = new Promise((resolve) => {
      const h = (method) => {
        if (method === "Tracing.tracingComplete") {
          cdp.offEvent(h);
          resolve();
        }
      };
      cdp.onEvent(h);
    });
    await cdp.send("Tracing.end");
    await Promise.race([
      complete,
      delay(10000, undefined, { ref: false }).then(() => {
        throw new Error("trace completion timeout");
      }),
    ]);
    const { profile } = await cdp.send("Profiler.stop");
    // Ending a trace resets the flat session's domain state; re-enable Page
    // and Runtime so subsequent navigation keeps working.
    try {
      await cdp.send("Page.enable");
      await cdp.send("Runtime.enable");
    } catch (error) {
      console.warn("Could not re-enable page domains", error);
    }
    await delay(400);
    const events = traceChunks;
    const phases = analyzeTrace(events, mapOrig);
    fs.writeFileSync(path.join(OUT, `${name}.trace.json`), JSON.stringify({ traceEvents: events }));
    fs.writeFileSync(path.join(OUT, `${name}.cpuprofile`), JSON.stringify(profile));
    fs.writeFileSync(
      path.join(OUT, `${name}.bottomup.json`),
      JSON.stringify(bottomUp(profile), null, 1),
    );
    fs.writeFileSync(path.join(OUT, `${name}.phases.json`), JSON.stringify(phases, null, 1));
    const pre = phases.map((p) => p.prePaintMs).sort((a, b) => a - b);
    const post = phases.map((p) => p.postPaintMs).sort((a, b) => a - b);
    console.log(
      `${name}: ${phases.length} switches; prePaint p50=${pre[Math.floor(pre.length / 2)]}ms postPaint p50=${post[Math.floor(post.length / 2)]}ms frames-before-paint p50=${phases.map((p) => p.framesBeforePaint).sort((a, b) => a - b)[Math.floor(phases.length / 2)]}`,
    );
    return phases;
  }

  async function gotoSession(id) {
    await cdp.send("Page.navigate", { url: `${BASE}/c/${id}` });
    await evalNow(cdp, `window.__waitSettled(${JSON.stringify(MARKERS[id])}, 30000, 250)`, true);
  }
  async function clickRow(title) {
    const rect = await evalNow(
      cdp,
      `(() => {
      const wanted = ${JSON.stringify(title)};
      const btns = [...document.querySelectorAll("button, [role=button], a, li, div")].filter((el) => {
        const t = (el.getAttribute("aria-label") || el.textContent || "").trim();
        if (t !== wanted) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.x < 420;
      });
      const el = btns.at(-1);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + Math.min(r.height / 2, 12) };
    })()`,
    );
    if (!rect) throw new Error(`row not found: ${title}`);
    await cdp.send("Input.dispatchMouseEvent", {
      type: "mousePressed",
      ...rect,
      button: "left",
      clickCount: 1,
    });
    await cdp.send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      ...rect,
      button: "left",
      clickCount: 1,
    });
  }
  async function switchTo(id, requireRetained = true) {
    const marker = MARKERS[id];
    const before = await globalThis
      .fetch(`${BASE}/__perf__/counters`)
      .then((response) => response.json());
    if (requireRetained && !before.activeStreams[id])
      throw new Error(`target ${id} is not retained`);
    await evalNow(cdp, `window.__resetPerf(${JSON.stringify(marker)})`);
    const settled = evalNow(
      cdp,
      `window.__waitSettled(${JSON.stringify(marker)}, 30000, 250)`,
      true,
    );
    await clickRow(TITLES[id]);
    const result = await settled;
    const after = await globalThis
      .fetch(`${BASE}/__perf__/counters`)
      .then((response) => response.json());
    if (
      result.markerTs < 0 ||
      (requireRetained && after.streamOpens[id] !== before.streamOpens[id])
    )
      throw new Error(`invalid warm switch to ${id}`);
  }

  const rotation = ["conv_perf_a", "conv_perf_b", "conv_perf_d"];
  await gotoSession(rotation[0]);
  await forEachSequential(rotation.slice(1), (id) => switchTo(id, false));
  await startTrace();
  await forEachSequential(
    Array.from({ length: 9 }, (_, index) => index),
    (index) => switchTo(rotation[index % rotation.length]),
  );
  const phases = await stopTrace("warm-closed");
  fs.writeFileSync(
    path.join(OUT, "phases-summary.json"),
    JSON.stringify({ warmClosed: phases }, null, 1),
  );

  await cdp.close();
}

try {
  await main();
} finally {
  child.kill("SIGTERM");
  if (child.exitCode === null && child.signalCode === null) {
    await Promise.race([once(child, "exit"), delay(5000)]);
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }
  fs.rmSync(PROFILE_DIR, { recursive: true, force: true });
}
