// Run one mocked web performance scenario per fresh browser profile.

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import {
  armOrder,
  cdpConnect,
  ensurePortFree,
  findChrome,
  forEachSequential,
  waitHttp,
} from "./cdp.mjs";
import { verifyFixtures } from "./verify.mjs";

const args = process.argv.slice(2);
const argOf = (name, fb) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fb;
};
let BASE = argOf("--url", "http://127.0.0.1:5299");
const OUT = path.resolve(
  argOf("--out", path.join(path.dirname(fileURLToPath(import.meta.url)), "out")),
);
const SCENARIOS = (argOf("--scenarios", "switch") || "").split(",").filter(Boolean);
const CYCLES = Number(argOf("--cycles", 0)) || 0;
const CHROME = findChrome(argOf("--chrome"));
const CPU_THROTTLE = Number(argOf("--throttle", "1"));
const DBG_PORT = Number(argOf("--cdp-port", "9336"));
const DBG_PORT_B = Number(argOf("--cdp-port-b", "9337"));
const ATTRIBUTION = args.includes("--attribution");
const AB_URL = argOf("--ab-url", "");
const validScenarios = [
  "switch",
  "switchEvicting",
  "switchFiles",
  "switchChanges",
  "switchCollapsedFile",
  "cold",
  "idle",
  "stream",
  "deepHistory",
  "ab",
];
if (
  SCENARIOS.length !== 1 ||
  !validScenarios.includes(SCENARIOS[0]) ||
  (SCENARIOS[0] === "ab" && !AB_URL)
)
  throw new Error("choose exactly one valid scenario");
class WarmSampleInvalid extends Error {}

const SESSIONS = ["conv_perf_a", "conv_perf_b", "conv_perf_c", "conv_perf_d", "conv_perf_e"];
// HTTP/1.1 permits only three retained streams in the app's registry.
const ROTATION = ["conv_perf_a", "conv_perf_b", "conv_perf_d"];
const MARKERS = {
  conv_perf_a: "SESSMARK-RF_A",
  conv_perf_b: "SESSMARK-RF_B",
  conv_perf_c: "SESSMARK-RF_C",
  conv_perf_d: "SESSMARK-RF_D",
  conv_perf_e: "SESSMARK-RF_E",
};
const OLD_MARKERS = {
  conv_perf_a: "SESSOLD-A",
  conv_perf_b: "SESSOLD-B",
};
const TITLES = {
  conv_perf_a: "Perf A — extreme streaming replay",
  conv_perf_b: "Perf B — docs rewrite",
  conv_perf_c: "Perf C — refactor pass",
  conv_perf_d: "Perf D — data pipeline",
  conv_perf_e: "Perf E — migration",
};

fs.mkdirSync(OUT, { recursive: true });

// ── In-page instrumentation (installed before app scripts on every document) ──
const INIT_SCRIPT = `
(() => {
  window.__perf = { longTasks: [], lafs: [], events: [], commits: [], requests: [], placeholder: [], placeholderPainted: [], lcp: 0, clickTs: -1, pointerTs: -1, markerTs: {}, dirty: false };
  if (${ATTRIBUTION}) {
    Error.stackTraceLimit = 50;
    const nativeFetch = window.fetch;
    window.fetch = function (...args) {
      const started = performance.now();
      const stack = new Error().stack;
      const pending = nativeFetch.apply(this, args);
      pending.then((response) => {
        window.__perf.requests.push({ url: response.url, started, ended: performance.now(), stack });
      }, () => {});
      return pending;
    };
  }
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__perf.longTasks.push({ s: e.startTime, d: e.duration });
    }).observe({ type: "longtask", buffered: true });
  } catch {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__perf.lafs.push({ s: e.startTime, d: e.duration });
    }).observe({ type: "long-animation-frame", buffered: true });
  } catch {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__perf.events.push({ s: e.startTime, d: e.duration, id: e.interactionId, name: e.name });
    }).observe({ type: "event", durationThreshold: 16, buffered: true });
  } catch {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) if (e.startTime > (window.__perf.lcp ?? 0)) window.__perf.lcp = e.startTime;
    }).observe({ type: "largest-contentful-paint", buffered: true });
  } catch {}
  // Keep focus and pointer work separate from the click-anchored windows.
  window.addEventListener("pointerdown", () => { window.__perf.pointerTs = performance.now(); }, { capture: true });
  window.addEventListener("click", () => { window.__perf.clickTs = performance.now(); }, { capture: true });
  // Marker watch: DOM mutations mark dirty; a 40 ms interval re-checks
  // body.textContent (no style/layout) only when dirty.
  window.__markerSeen = (marker) => {
    const seen = window.__perf.markerTs[marker];
    return seen !== undefined ? seen : -1;
  };
  setInterval(() => {
    if (!window.__perf.dirty) return;
    window.__perf.dirty = false;
    const text = document.body?.textContent ?? "";
    for (const marker of window.__perf.wantedMarkers ?? []) {
      if (window.__perf.markerTs[marker] === undefined && text.includes(marker)) {
        window.__perf.markerTs[marker] = performance.now();
      }
    }
  }, 40);
  const placeholderText = "Loading conversation…";
  const mo = new MutationObserver((records) => {
    window.__perf.dirty = true;
    const matches = (node) => {
      if (node.nodeType === Node.TEXT_NODE) return node.nodeValue?.trim() === placeholderText;
      return node.nodeType === Node.ELEMENT_NODE && [...node.childNodes].some((child) => child.nodeType === Node.TEXT_NODE && child.nodeValue?.trim() === placeholderText);
    };
    const added = records.flatMap((record) => [...record.addedNodes]).find(matches);
    const removed = records.some((record) => [...record.removedNodes].some(matches));
    const observedAt = added || removed ? performance.now() : 0;
    if (added) {
      window.__perf.placeholder.push({ kind: "mount", ts: observedAt });
      requestAnimationFrame(() => setTimeout(() => {
        if (added.isConnected) window.__perf.placeholderPainted.push(performance.now());
      }, 0));
    }
    if (removed) window.__perf.placeholder.push({ kind: "unmount", ts: observedAt });
  });
  const armMo = () => mo.observe(document.body ?? document.documentElement, { childList: true, subtree: true, characterData: true });
  if (document.body) armMo(); else document.addEventListener("DOMContentLoaded", armMo, { once: true });
  // React commit counter: the renderer finds this hook even in production builds.
  window.__reactCommits = 0;
  window.__rendered = {};
  const previousFibers = new WeakMap();
  const hook = {
    renderers: new Map(),
    isDisabled: false,
    supportsFiber: true,
    onCommitFiberRoot(_renderer, root) {
      window.__reactCommits++;
      const commit = { ts: performance.now() };
      window.__perf.commits.push(commit);
      if (!${ATTRIBUTION}) return;
      commit.renders = {};
      commit.mounts = [];
      commit.stack = new Error().stack;
      const previous = previousFibers.get(root) ?? new Set();
      const current = new Set();
      const stack = [root.current];
      while (stack.length) {
        const fiber = stack.pop();
        current.add(fiber);
        if (fiber.sibling) stack.push(fiber.sibling);
        if (fiber.child) stack.push(fiber.child);
        if (![0, 1, 11, 14, 15, 16].includes(fiber.tag) || !fiber.type || previous.has(fiber)) continue;
        const existed = previous.has(fiber.alternate);
        if (existed && !(fiber.flags & 1)) continue;
        const type = fiber.type?.type ?? fiber.type?.render ?? fiber.type;
        const name = type?.displayName || type?.name || "(anonymous)";
        window.__rendered[name] = (window.__rendered[name] ?? 0) + 1;
        commit.renders[name] = (commit.renders[name] ?? 0) + 1;
        if (!existed) commit.mounts.push(name);
      }
      previousFibers.set(root, current);
    },
    onCommitFiberUnmount() {},
    inject() { return 1; },
    onScheduleFiberRoot() {},
  };
  Object.defineProperty(window, "__REACT_DEVTOOLS_GLOBAL_HOOK__", { value: hook, configurable: true });
})();
`;

const SETTLED_FN = `
window.__awaitClick = async (sinceTs, timeoutMs = 15000) => {
  const t0 = performance.now();
  while ((window.__perf.clickTs ?? -1) <= sinceTs) {
    if (performance.now() - t0 > timeoutMs) return -1;
    await new Promise((r) => setTimeout(r, 5));
  }
  return window.__perf.clickTs;
};
window.__waitSettled = async (marker, opts = {}) => {
  const { timeoutMs = 20000, quietMs = 250, sinceTs = -1 } = opts;
  const raf = () => new Promise((r) => requestAnimationFrame(r));
  const macrotask = () => new Promise((r) => setTimeout(r, 0));
  // 1. anchor on the click
  const clickTs = sinceTs > 0 ? sinceTs : await window.__awaitClick(sinceTs, timeoutMs);
  if (clickTs < 0) return { clickTs: -1, markerTs: -1, paintedTs: -1, settleTs: -1, timeout: "click" };
  // 2. wait for the marker (mutation-driven textContent check; no layout)
  let markerTs = -1;
  const deadline = clickTs + timeoutMs;
  window.__perf.wantedMarkers = [...new Set([...(window.__perf.wantedMarkers ?? []), marker])];
  while (markerTs < 0) {
    if (performance.now() > deadline) return { clickTs, markerTs: -1, paintedTs: -1, settleTs: -1, timeout: "marker" };
    await new Promise((r) => setTimeout(r, 15));
    markerTs = window.__markerSeen(marker);
  }
  // 3. content painted: first rAF + one macrotask after the marker is present
  await raf();
  await macrotask();
  const paintedTs = performance.now();
  // 4. settled: no long task / LoAF ending within the last quietMs
  const busyEndNow = () => {
    let end = -1;
    for (const lt of window.__perf.longTasks) end = Math.max(end, lt.s + lt.d);
    for (const lf of window.__perf.lafs) end = Math.max(end, lf.s + lf.d);
    return end;
  };
  let busyEnd = busyEndNow();
  let settleTs = -1;
  while (performance.now() < paintedTs + timeoutMs) {
    await new Promise((r) => setTimeout(r, 25));
    busyEnd = busyEndNow();
    if (busyEnd < 0 || performance.now() - busyEnd > quietMs) { settleTs = performance.now(); break; }
  }
  return { clickTs, markerTs, paintedTs, settleTs, busyEnd };
};
window.__resetPerf = () => {
  window.__perf.longTasks.length = 0;
  window.__perf.lafs.length = 0;
  window.__perf.events.length = 0;
  window.__perf.commits.length = 0;
  window.__perf.requests.length = 0;
  window.__perf.placeholder.length = 0;
  window.__perf.placeholderPainted.length = 0;
  window.__perf.markerTs = {};
  window.__perf.clickTs = -1;
  window.__perf.pointerTs = -1;
  window.__reactCommits = 0;
  window.__rendered = {};
};
`;

async function inPageEval(cdp, expression, awaitPromise = false) {
  const res = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
  });
  if (res.exceptionDetails) throw new Error(`eval failed: ${res.exceptionDetails.text}`);
  return res.result?.value;
}

function busyStats(entries, t0, t1) {
  const inWin = entries.filter((lt) => lt.s >= t0 && lt.s < t1);
  const total = inWin.reduce((a, lt) => a + lt.d, 0);
  return {
    count: inWin.length,
    totalMs: Math.round(total * 10) / 10,
    maxMs: inWin.length ? Math.round(Math.max(...inWin.map((lt) => lt.d)) * 10) / 10 : 0,
  };
}

// ── Browser lifecycle ────────────────────────────────────────
const browsers = [];
async function launchBrowser(port) {
  await ensurePortFree(port);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "perf-bench-"));
  const child = spawn(
    CHROME,
    [
      `--user-data-dir=${profile}`,
      `--remote-debugging-port=${port}`,
      "--headless=new",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-features=Translate,BackForwardCache",
      "--window-size=1600,1000",
      "--enable-precise-memory-info",
      "--disable-component-update",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  browsers.push({ child, profile });
}
await verifyFixtures(BASE, CHROME, DBG_PORT, path.join(OUT, "fixture-check-a.json"));
if (AB_URL)
  await verifyFixtures(AB_URL, CHROME, DBG_PORT_B, path.join(OUT, "fixture-check-b.json"));
await launchBrowser(DBG_PORT);
process.once("SIGTERM", () => browsers.forEach(({ child }) => child.kill("SIGTERM")));
process.once("SIGINT", () => browsers.forEach(({ child }) => child.kill("SIGTERM")));

const results = {
  startedAt: new Date().toISOString(),
  base: BASE,
  cpuThrottle: CPU_THROTTLE,
  cycles: CYCLES || "per-scenario default",
  scenarios: {},
};

async function perfMetrics(cdp) {
  const { metrics } = await cdp.send("Performance.getMetrics");
  const out = {};
  for (const m of metrics) out[m.name] = m.value;
  return out;
}
function metricsDelta(after, before) {
  const d = {};
  for (const k of [
    "ScriptDuration",
    "TaskDuration",
    "LayoutDuration",
    "RecalcStyleDuration",
    "LayoutCount",
    "RecalcStyleCount",
  ]) {
    d[k] = Math.round(((after[k] ?? 0) - (before[k] ?? 0)) * 1000) / 1000;
  }
  d.JSHeapUsedSizeAfterMB = Math.round(((after.JSHeapUsedSize ?? 0) / 1048576) * 10) / 10;
  return d;
}

async function main() {
  const version = await waitHttp(`http://127.0.0.1:${DBG_PORT}/json/version`);
  let cdp = cdpConnect(version.webSocketDebuggerUrl);
  const clients = [cdp];
  await cdp.ready;
  const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
  cdp.setDefaultSession(sessionId);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Performance.enable");
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU_THROTTLE });
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: INIT_SCRIPT + SETTLED_FN });

  const consoleErrors = [];
  const recordErrors = (method, params) => {
    if (method === "Runtime.exceptionThrown") {
      consoleErrors.push(
        params.exceptionDetails?.exception?.description ?? params.exceptionDetails?.text ?? "?",
      );
    } else if (method === "Runtime.consoleAPICalled" && params.type === "error") {
      consoleErrors.push((params.args ?? []).map((a) => a.value ?? a.description ?? "").join(" "));
    }
  };
  cdp.onEvent(recordErrors);

  const page = { eval: (expr, awaitPromise) => inPageEval(cdp, expr, awaitPromise) };
  const cyclesFor = (def) => CYCLES || def;
  const indices = (count) => Array.from({ length: count }, (_, index) => index);

  async function gotoSession(id, attempt = 0) {
    await cdp.send("Page.navigate", { url: `${BASE}/c/${id}` });
    const settled = await page.eval(
      `window.__waitSettled(${JSON.stringify(MARKERS[id])}, { timeoutMs: 30000, sinceTs: performance.now() - 1 })`,
      true,
    );
    if (settled && settled.markerTs > 0) return settled;
    process.stdout.write(
      `gotoSession ${id} retry ${attempt}: ${JSON.stringify(settled).slice(0, 160)}\n`,
    );
    if (attempt < 2) return gotoSession(id, attempt + 1);
    throw new Error(`gotoSession: ${id} never settled`);
  }

  async function clickSidebarRow(title) {
    const rect = await page.eval(`(() => {
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
    })()`);
    if (!rect) throw new Error(`sidebar row not found: ${title}`);
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

  async function openRail() {
    const isOpen = await page.eval(
      `!![...document.querySelectorAll("button")].find(x=>(x.getAttribute("aria-label")||"")==="Collapse right panel")`,
    );
    if (isOpen) return;
    await page.eval(`(() => {
      const b = [...document.querySelectorAll("button")].find(x=>(x.getAttribute("aria-label")||"").includes("Expand right panel"));
      if (b) b.click();
    })()`);
    await delay(600);
  }

  async function closeRail() {
    await page.eval(`(() => {
      const button = [...document.querySelectorAll("button")].find(x => (x.getAttribute("aria-label") || "").includes("Collapse right panel"));
      if (button) button.click();
    })()`);
    await delay(600);
    const stillOpen = await page.eval(
      `!![...document.querySelectorAll("button")].find(x => (x.getAttribute("aria-label") || "").includes("Collapse right panel"))`,
    );
    if (stillOpen) throw new Error("could not collapse right panel");
  }

  async function clickRailTab(label) {
    const rect = await page.eval(`(() => {
      const tab = document.querySelector('[aria-label="Workspace"] [data-workspace-tab="${label.toLowerCase()}"]');
      if (!tab) return null;
      const bounds = tab.getBoundingClientRect();
      return { x: bounds.x + bounds.width / 2, y: bounds.y + bounds.height / 2 };
    })()`);
    if (!rect) throw new Error(`workspace tab not found: ${label}`);
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

  async function assertRailTab(tab) {
    const state = await page.eval(`(() => {
      const rail = document.querySelector('[aria-label="Workspace"]');
      const tab = rail?.querySelector('[data-workspace-tab="${tab}"]');
      return { hidden: rail?.getAttribute("aria-hidden"), selected: tab?.getAttribute("aria-selected") };
    })()`);
    if (state.hidden !== "false" || state.selected !== "true")
      throw new Error(`workspace ${tab} tab not open: ${JSON.stringify(state)}`);
  }

  // Shared switch measurement. Pre-condition: page settled somewhere.
  async function measureSwitch({ target, label, extraMarker = null }) {
    const warm = [
      "rail-closed",
      "rail-files",
      "rail-changes",
      "rail-closed-file",
      "a",
      "b",
    ].includes(label);
    const evicting = label === "evicting";
    const beforeStreams =
      warm || evicting
        ? await globalThis.fetch(`${BASE}/__perf__/counters`).then((response) => response.json())
        : null;
    if (warm && !beforeStreams.activeStreams[target])
      throw new WarmSampleInvalid(`target ${target} has no retained stream before warm switch`);
    await page.eval(`window.__resetPerf()`);
    const before = await perfMetrics(cdp);
    const marker = extraMarker ?? MARKERS[target];
    // Armed BEFORE the click; anchors on the click itself.
    const settlePromise = page.eval(
      `window.__waitSettled(${JSON.stringify(marker)}, { timeoutMs: 25000 })`,
      true,
    );
    await clickSidebarRow(TITLES[target]);
    const ts = await settlePromise;
    if (!ts || ts.timeout || ts.paintedTs < 0)
      throw new Error(`switch to ${target} failed: ${JSON.stringify(ts)}`);
    await page.eval(
      `new Promise((resolve) => setTimeout(resolve, Math.max(0, ${ts.clickTs} + 1500 - performance.now())))`,
      true,
    );
    const snap = await page.eval(`(() => {
      const events = window.__perf.events.filter((e) => e.id > 0 && e.s >= (window.__perf.clickTs ?? 0) - 1);
      const clickEv = events.length ? events[events.length - 1] : null;
      return {
        domNodes: document.getElementsByTagName("*").length,
        heapMB: Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1048576 * 10) / 10,
        interactionId: clickEv?.id ?? null,
        interactionStart: clickEv?.s ?? null,
        interactionDur: clickEv?.d ?? null,
        pointerTs: window.__perf.pointerTs,
        commits: window.__perf.commits,
        requests: window.__perf.requests,
        railHidden: document.querySelector('[aria-label="Workspace"]')?.getAttribute("aria-hidden"),
        pathname: location.pathname,
        placeholder: window.__perf.placeholder,
        placeholderPainted: window.__perf.placeholderPainted,
      };
    })()`);
    const lts = await page.eval("window.__perf.longTasks");
    const lafs = await page.eval("window.__perf.lafs");
    if (snap.pathname !== `/c/${target}`)
      throw new Error(`switch target mismatch: expected ${target}, got ${snap.pathname}`);
    if (["rail-closed", "evicting", "a", "b"].includes(label) && snap.railHidden !== "true")
      throw new Error(`workspace rail is not collapsed: ${snap.railHidden}`);
    const commitEvents = snap.commits.filter(
      (event) => event.ts >= ts.clickTs && event.ts < ts.clickTs + 1500,
    );
    const pointerCommitEvents = snap.commits.filter(
      (event) => event.ts >= snap.pointerTs && event.ts < ts.clickTs,
    );
    const placeholderMount = snap.placeholder.find(
      (event) => event.kind === "mount" && event.ts >= ts.clickTs,
    );
    const placeholderUnmount =
      placeholderMount &&
      snap.placeholder.find((event) => event.kind === "unmount" && event.ts >= placeholderMount.ts);
    const placeholderPaint =
      placeholderMount &&
      snap.placeholderPainted.find(
        (time) =>
          time >= placeholderMount.ts && (!placeholderUnmount || time < placeholderUnmount.ts),
      );
    const afterStreams =
      warm || evicting
        ? await globalThis.fetch(`${BASE}/__perf__/counters`).then((response) => response.json())
        : null;
    if (
      warm &&
      (afterStreams.streamOpens[target] !== beforeStreams.streamOpens[target] ||
        !afterStreams.activeStreams[target])
    )
      throw new WarmSampleInvalid(
        `target ${target} re-bound during warm switch: ${JSON.stringify({ beforeStreams, afterStreams })}`,
      );
    if (
      evicting &&
      (afterStreams.streamOpens[target] ?? 0) <= (beforeStreams.streamOpens[target] ?? 0)
    )
      throw new Error(`target ${target} did not re-bind during evicting rotation`);
    return {
      label,
      target,
      ...(warm || evicting
        ? { streamOpens: beforeStreams.streamOpens[target], retainedStream: warm }
        : {}),
      cold: evicting,
      clickTs: ts.clickTs,
      markerTs: ts.markerTs,
      paintedTs: ts.paintedTs,
      settleTs: ts.settleTs,
      busyEnd: ts.busyEnd,
      clickToMarkerMs: ts.markerTs > 0 ? Math.round((ts.markerTs - ts.clickTs) * 10) / 10 : null,
      clickToPaintedMs: ts.paintedTs > 0 ? Math.round((ts.paintedTs - ts.clickTs) * 10) / 10 : null,
      // Work-done time: the last busy signal (painted OR long task/LoAF end),
      // i.e. when the main thread actually finished the switch's work.
      clickToWorkDoneMs:
        ts.paintedTs > 0
          ? Math.round(
              (Math.max(ts.paintedTs, ts.busyEnd > ts.clickTs ? ts.busyEnd : 0) - ts.clickTs) * 10,
            ) / 10
          : null,
      clickToSettledMs: ts.settleTs > 0 ? Math.round((ts.settleTs - ts.clickTs) * 10) / 10 : null,
      busyDuringSwitch: busyStats(lts, ts.clickTs, ts.settleTs > 0 ? ts.settleTs : ts.clickTs + 1),
      lafBusy: busyStats(lafs, ts.clickTs, ts.settleTs > 0 ? ts.settleTs : ts.clickTs + 1),
      interaction: snap.interactionDur,
      interactionStartDelay:
        snap.interactionStart !== null
          ? Math.round((snap.interactionStart - ts.clickTs) * 10) / 10
          : null,
      commits: commitEvents.length,
      commitsToPaint: snap.commits.filter(
        (event) => event.ts >= ts.clickTs && event.ts <= ts.paintedTs,
      ).length,
      commitsPointerToClick: pointerCommitEvents.length,
      pointerToClickMs: snap.pointerTs >= 0 ? ts.clickTs - snap.pointerTs : null,
      placeholder: {
        mounted: Boolean(placeholderMount),
        painted: Boolean(placeholderPaint),
        mountedMs:
          placeholderMount && placeholderUnmount && placeholderUnmount.ts !== placeholderMount.ts
            ? placeholderUnmount.ts - placeholderMount.ts
            : null,
        visibleMs: !placeholderPaint
          ? 0
          : placeholderUnmount
            ? placeholderUnmount.ts - placeholderPaint
            : null,
      },
      ...(ATTRIBUTION
        ? {
            commitEvents,
            pointerCommitEvents,
            requests: snap.requests.filter(
              (request) => request.started >= ts.clickTs && request.started < ts.clickTs + 1500,
            ),
          }
        : {}),
      domNodes: snap.domNodes,
      heapMB: snap.heapMB,
      cdp: metricsDelta(await perfMetrics(cdp), before),
    };
  }

  // Separate browser processes isolate the arms' caches and renderer heaps.
  if (AB_URL) {
    const arms = [{ name: "a", url: BASE, sessionId, client: cdp }];
    await launchBrowser(DBG_PORT_B);
    const versionB = await waitHttp(`http://127.0.0.1:${DBG_PORT_B}/json/version`);
    const clientB = cdpConnect(versionB.webSocketDebuggerUrl);
    clients.push(clientB);
    clientB.onEvent(recordErrors);
    await clientB.ready;
    const second = await clientB.send("Target.createTarget", { url: "about:blank" });
    const attached = await clientB.send("Target.attachToTarget", {
      targetId: second.targetId,
      flatten: true,
    });
    arms.push({ name: "b", url: AB_URL, sessionId: attached.sessionId, client: clientB });
    await forEachSequential(arms, async (arm) => {
      BASE = arm.url;
      cdp = arm.client;
      cdp.setDefaultSession(arm.sessionId);
      await cdp.send("Page.enable");
      await cdp.send("Runtime.enable");
      await cdp.send("Network.enable");
      await cdp.send("Performance.enable");
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU_THROTTLE });
      if (arm.name === "b")
        await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
          source: INIT_SCRIPT + SETTLED_FN,
        });
      await cdp.send("Page.bringToFront");
      await gotoSession(ROTATION[0]);
      await closeRail();
      await forEachSequential(ROTATION.slice(1), (id) =>
        measureSwitch({ target: id, label: "warmup" }),
      );
      await delay(1500);
    });
    const samples = { a: [], b: [] };
    const count = Number(argOf("--samples", "30"));
    if (!Number.isInteger(count) || count < 1)
      throw new Error("--samples must be a positive integer");
    let discardedPairs = 0;
    async function rewarm(arm, target) {
      BASE = arm.url;
      cdp = arm.client;
      cdp.setDefaultSession(arm.sessionId);
      await cdp.send("Page.bringToFront");
      const others = ROTATION.filter((id) => id !== target);
      await forEachSequential([...others, target, others[0]], async (id) => {
        const current = await page.eval("location.pathname");
        if (current !== `/c/${id}`) await measureSwitch({ target: id, label: "warmup" });
      });
    }
    async function collectPair(index, attempt = 0) {
      const target = ROTATION[index % ROTATION.length];
      const pair = [];
      try {
        await forEachSequential(armOrder(index), async (name) => {
          const arm = arms.find((candidate) => candidate.name === name);
          BASE = arm.url;
          cdp = arm.client;
          cdp.setDefaultSession(arm.sessionId);
          await cdp.send("Page.bringToFront");
          await forEachSequential(arms, async (visibleArm) => {
            cdp = visibleArm.client;
            cdp.setDefaultSession(visibleArm.sessionId);
            if (await page.eval("document.hidden"))
              throw new Error(`arm ${visibleArm.name} is hidden`);
          });
          cdp = arm.client;
          cdp.setDefaultSession(arm.sessionId);
          const sample = await measureSwitch({ target, label: arm.name });
          sample.orderPosition = armOrder(index)[0] === arm.name ? "first" : "second";
          pair.push({ name, sample });
        });
      } catch (error) {
        if (!(error instanceof WarmSampleInvalid) || attempt >= 2) throw error;
        discardedPairs++;
        console.warn(`Discarding pair ${index + 1}: ${error.message}`);
        await forEachSequential(arms, (arm) => rewarm(arm, target));
        return collectPair(index, attempt + 1);
      }
      pair.forEach(({ name, sample }) => {
        samples[name].push(sample);
        process.stdout.write(`AB ${name} ${index + 1}/${count}: ${sample.clickToPaintedMs}ms\n`);
      });
    }
    await forEachSequential(indices(count), (index) => collectPair(index));
    results.discardedPairs = discardedPairs;
    results.scenarios.ab = samples;
  }

  // ── Scenario: switch (rail closed) ─────────────────────────
  if (!AB_URL && (SCENARIOS.includes("switch") || SCENARIOS.includes("switchEvicting"))) {
    const evicting = SCENARIOS.includes("switchEvicting");
    const rotation = evicting ? SESSIONS : ROTATION;
    await gotoSession(rotation[0]);
    await closeRail();
    await forEachSequential(rotation.slice(1), (id) =>
      measureSwitch({ target: id, label: "warmup" }),
    );
    await delay(1500);

    const samples = [];
    const cycles = cyclesFor(evicting ? 6 : 10);
    await forEachSequential(indices(cycles), (cyc) =>
      forEachSequential(rotation, async (id) => {
        const s = await measureSwitch({ target: id, label: evicting ? "evicting" : "rail-closed" });
        samples.push(s);
        process.stdout.write(
          `switch cyc${cyc} ${id} marker=${s.clickToMarkerMs} painted=${s.clickToPaintedMs} settled=${s.clickToSettledMs}ms\n`,
        );
      }),
    );
    results.scenarios[evicting ? "switchEvicting" : "switch"] = samples;

    // Cold switches via reload: fresh page binds only its own URL's session.
    if (!ATTRIBUTION && !evicting) {
      const coldSamples = [];
      const coldN = Math.max(cycles, 2) * 2;
      await forEachSequential(indices(coldN), async (i) => {
        const anchor = SESSIONS[i % SESSIONS.length];
        const target = SESSIONS[(i + 1) % SESSIONS.length];
        await gotoSession(anchor);
        await cdp.send("Page.navigate", { url: `${BASE}/c/${anchor}` });
        await delay(2500);
        const s = await measureSwitch({ target, label: "cold-reload" });
        coldSamples.push({ ...s, cold: true, viaReload: true, interaction: null });
        process.stdout.write(`switch-cold ${i} ${target} settled=${s.clickToSettledMs}ms\n`);
      });
      results.scenarios.switchCold = coldSamples;
    }
  }

  // ── Rail-open setup helpers ────────────────────────────────
  async function expandAllRootDirs(maxExpands = 10) {
    async function expand(remaining) {
      if (!remaining) return;
      const clicked = await page.eval(`(() => {
        const rail = document.querySelector('[aria-label="Workspace"]');
        const btn = [...(rail?.querySelectorAll("button[aria-expanded]") ?? [])]
          .filter(el => el.getAttribute("aria-expanded") === "false")
          .filter(el => el.getBoundingClientRect().width > 0)[0];
        if (!btn) return false;
        btn.click();
        return true;
      })()`);
      if (!clicked) return;
      await delay(700);
      return expand(remaining - 1);
    }
    await expand(maxExpands);
    return page.eval(
      `document.querySelector('[aria-label="Workspace"]')?.querySelectorAll("button[aria-expanded]").length ?? 0`,
    );
  }

  async function setupFilesPanel(id) {
    await openRail();
    await clickRailTab("Files");
    await delay(1200);
    const rows = await expandAllRootDirs();
    await assertRailTab("files");
    const expanded = await page.eval(
      `document.querySelector('[aria-label="Workspace"]')?.querySelectorAll('button[aria-expanded="true"]').length ?? 0`,
    );
    if (!expanded) throw new Error(`files tree has no expanded directories for ${id}`);
    process.stdout.write(`setupFiles ${id}: expandable rows=${rows} expanded=${expanded}\n`);
  }

  // Open a file through the app's own ui.open_file SSE event (runner-native).
  async function openFileViaSse(conversationId, filePath, waitMs = 2500) {
    const ok = await globalThis
      .fetch(`${BASE}/__perf__/openfile`, {
        method: "POST",
        body: JSON.stringify({ session: conversationId, path: filePath }),
      })
      .then((r) => r.json())
      .catch(() => ({ ok: false }));
    await delay(waitMs);
    const visible = await page.eval(`(() => {
      const rail = document.querySelector('[aria-label="Workspace"]');
      return rail ? (rail.textContent || "").includes(${JSON.stringify(filePath.split("/").at(-1))}) : false;
    })()`);
    return { ok: ok.ok === true, visible };
  }

  // ── Scenario: collapsed rail retaining session D's large file ──
  if (SCENARIOS.includes("switchCollapsedFile")) {
    await gotoSession(ROTATION[0]);
    await forEachSequential(ROTATION.slice(1), (id) =>
      measureSwitch({ target: id, label: "warmup" }),
    );
    await openRail();
    await clickRailTab("Files");
    const file = "dialog_7/stepper_00002.md";
    const opened = await openFileViaSse("conv_perf_d", file, 5000);
    if (!opened.ok || !opened.visible)
      throw new Error(`could not open session D file: ${JSON.stringify(opened)}`);
    await closeRail();
    await measureSwitch({ target: "conv_perf_b", label: "warmup" });
    const samples = [];
    await forEachSequential(indices(cyclesFor(10)), (cycle) =>
      forEachSequential(ROTATION, async (id) => {
        const sample = await measureSwitch({ target: id, label: "rail-closed-file" });
        if (id === "conv_perf_d") {
          sample.viewerRetained = await page.eval(`(() => {
          const rail = document.querySelector('[aria-label="Workspace"]');
          const text = rail?.textContent || "";
          return { file: text.includes("stepper_00002.md"), content: text.includes("export async function"), hidden: rail?.getAttribute("aria-hidden") === "true" };
        })()`);
          if (
            !sample.viewerRetained.file ||
            !sample.viewerRetained.content ||
            !sample.viewerRetained.hidden
          )
            throw new Error(
              `session D viewer is not mounted and collapsed: ${JSON.stringify(sample.viewerRetained)}`,
            );
        }
        samples.push(sample);
        process.stdout.write(`switchCollapsedFile ${cycle} ${id}: ${sample.clickToPaintedMs}ms\n`);
      }),
    );
    results.scenarios.switchCollapsedFile = samples;
  }

  // ── Scenario: switch with Files tab open + expanded dirs ───
  if (SCENARIOS.includes("switchFiles")) {
    await gotoSession(ROTATION[0]);
    await forEachSequential(ROTATION, async (id) => {
      if (id !== ROTATION[0]) await measureSwitch({ target: id, label: "warmup" });
      await setupFilesPanel(id);
    });
    await delay(1500);
    const samples = [];
    const cycles = cyclesFor(10);
    await forEachSequential(indices(cycles), (cyc) =>
      forEachSequential(ROTATION, async (id) => {
        const s = await measureSwitch({ target: id, label: "rail-files" });
        await assertRailTab("files");
        samples.push(s);
        process.stdout.write(
          `switchFiles cyc${cyc} ${id} painted=${s.clickToPaintedMs} settled=${s.clickToSettledMs}ms dom=${s.domNodes}\n`,
        );
      }),
    );
    results.scenarios.switchFiles = samples;
  }

  // ── Scenario: switch with Changes tab open ──────────────────
  // The Changes surface is the folder tree with changed-file badges plus the
  // flat 120-file changed list. A programmatic diff open did not register a
  // viewer tab in this build (row clicks and ui.open_file both fail to mount
  // FileViewer here), so this scenario measures the Changes surface itself.
  if (SCENARIOS.includes("switchChanges")) {
    await gotoSession(ROTATION[0]);
    await forEachSequential(ROTATION, async (id) => {
      if (id !== ROTATION[0]) await measureSwitch({ target: id, label: "warmup" });
      await openRail();
      await clickRailTab("Changes");
      await delay(2000);
      const visible = await page.eval(`(() => {
        const rail = document.querySelector('[aria-label="Workspace"]');
        return rail ? (rail.textContent || "").includes("profiler_52.tsx") : false;
      })()`);
      await assertRailTab("changes");
      if (!visible) throw new Error(`changes list not visible for ${id}`);
      process.stdout.write(`setupChanges ${id} changesVisible=${visible}\n`);
    });
    await delay(2000);
    const samples = [];
    const cycles = cyclesFor(10);
    await forEachSequential(indices(cycles), (cyc) =>
      forEachSequential(ROTATION, async (id) => {
        const s = await measureSwitch({ target: id, label: "rail-changes" });
        await assertRailTab("changes");
        samples.push(s);
        process.stdout.write(
          `switchChanges cyc${cyc} ${id} painted=${s.clickToPaintedMs} settled=${s.clickToSettledMs}ms dom=${s.domNodes}\n`,
        );
      }),
    );
    results.scenarios.switchChanges = samples;
  }

  // ── Scenario: cold load ────────────────────────────────────
  if (SCENARIOS.includes("cold")) {
    const samples = [];
    const perSession = CYCLES || 4;
    await forEachSequential(indices(perSession), (i) =>
      forEachSequential(SESSIONS, async (id) => {
        await cdp.send("Network.clearBrowserCache");
        await page.eval(`window.__resetPerf()`).catch(() => {});
        const navStart = Date.now();
        await cdp.send("Page.navigate", { url: `${BASE}/c/${id}` });
        const settled = await page.eval(
          `window.__waitSettled(${JSON.stringify(MARKERS[id])}, { timeoutMs: 30000, sinceTs: performance.now() - 1 })`,
          true,
        );
        if (!settled || settled.timeout || settled.markerTs < 0 || settled.paintedTs < 0)
          throw new Error(`cold load ${id} failed: ${JSON.stringify(settled)}`);
        const snap = await page.eval(`(() => {
          const nav = performance.getEntriesByType("navigation")[0];
          return {
            lcp: window.__perf.lcp,
            dcl: nav ? nav.domContentLoadedEventEnd : null,
            domNodes: document.getElementsByTagName("*").length,
            heapMB: Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1048576 * 10) / 10,
            commits: window.__reactCommits,
          };
        })()`);
        const lts = await page.eval("window.__perf.longTasks");
        const after = await perfMetrics(cdp);
        samples.push({
          target: id,
          iter: i,
          // Chrome resets these duration counters on each document navigation.
          cdp: metricsDelta(after, {}),
          wallMs: Date.now() - navStart,
          markerTs: settled.markerTs,
          paintedTs: settled.paintedTs,
          settleTs: settled.settleTs,
          markerVisibleMs: settled.markerTs, // relative to navigation start (time origin)
          paintedMs: settled.paintedTs,
          lcpMs: snap.lcp,
          dclMs: snap.dcl,
          busy: busyStats(lts, 0, 1e12),
          commits: snap.commits,
          domNodes: snap.domNodes,
          heapMB: snap.heapMB, // performance.memory.usedJSHeapSize (precise mode)
        });
        process.stdout.write(
          `cold ${id} marker=${Math.round(settled.markerTs)} lcp=${Math.round(snap.lcp)} heap=${snap.heapMB}MB\n`,
        );
      }),
    );
    results.scenarios.cold = samples;
  }

  // ── Idle sampler shared by idle / idleViewer ───────────────
  async function idleSample(cdp2, seconds = 10) {
    await page.eval(`window.__resetPerf()`);
    const before = await perfMetrics(cdp2);
    const sample = await page.eval(
      `(async () => {
      const t0 = performance.now();
      const lt0 = window.__perf.longTasks.length;
      let dropped = 0;
      let last = await new Promise((r) => requestAnimationFrame(r));
      while (performance.now() - t0 < ${seconds * 1000}) {
        const now = await new Promise((r) => requestAnimationFrame(r));
        if (now - last > 32) dropped++;
        last = now;
      }
      const lts = window.__perf.longTasks.slice(lt0);
      return {
        busyPct: Math.round((lts.reduce((a, lt) => a + lt.d, 0) / (performance.now() - t0)) * 1000) / 10,
        longTasks: lts.length,
        droppedFrames: dropped,
      };
    })()`,
      true,
    );
    const after = await perfMetrics(cdp2);
    const guardrails = await page.eval(
      `({ commits: window.__reactCommits, heapMB: (performance.memory?.usedJSHeapSize ?? 0) / 1048576 })`,
    );
    return {
      ...sample,
      ...guardrails,
      cdp: metricsDelta(after, before),
      taskDurationMs: (after.TaskDuration - before.TaskDuration) * 1000,
      scriptDurationMs: (after.ScriptDuration - before.ScriptDuration) * 1000,
    };
  }

  // ── Scenario: idle steady state (rail closed) ──────────────
  if (SCENARIOS.includes("idle")) {
    const samples = [];
    const iters = CYCLES || 2;
    await forEachSequential(SESSIONS, (id) =>
      forEachSequential(indices(iters), async (i) => {
        await gotoSession(id);
        await delay(2000);
        samples.push({ target: id, iter: i, rail: "closed", ...(await idleSample(cdp)) });
        process.stdout.write(`idle ${id} task=${Math.round(samples.at(-1).taskDurationMs)}ms\n`);
      }),
    );
    results.scenarios.idle = samples;
  }

  // ── Streaming helper ───────────────────────────────────────
  async function streamSample(target, iter, extra = {}) {
    if (extra.history !== "full") await gotoSession(target);
    await delay(1500);
    await page.eval(`window.__resetPerf()`);
    const before = await perfMetrics(cdp);
    const startRes = await globalThis
      .fetch(`${BASE}/__perf__/stream/start`, {
        method: "POST",
        body: JSON.stringify({ session: target, duration_ms: 10000, events_per_sec: 20 }),
      })
      .then((r) => r.json());
    if (!startRes.ok || !startRes.total_events)
      throw new Error(`stream injection failed: ${JSON.stringify(startRes)}`);
    const sample = await page.eval(
      `(async () => {
      const t0 = performance.now();
      const lt0 = window.__perf.longTasks.length;
      const commit0 = window.__reactCommits;
      let dropped = 0;
      let last = await new Promise((r) => requestAnimationFrame(r));
      while (performance.now() - t0 < 10500) {
        const now = await new Promise((r) => requestAnimationFrame(r));
        if (now - last > 32) dropped++;
        last = now;
      }
      const lts = window.__perf.longTasks.slice(lt0);
      return {
        busyPct: Math.round((lts.reduce((a, lt) => a + lt.d, 0) / (performance.now() - t0)) * 1000) / 10,
        longTasks: lts.length,
        longTaskMax: lts.length ? Math.max(...lts.map((l) => l.d)) : 0,
        droppedFrames: dropped,
        commits: window.__reactCommits - commit0,
      };
    })()`,
      true,
    );
    const after = await perfMetrics(cdp);
    const stopRes = await globalThis
      .fetch(`${BASE}/__perf__/stream/stop`, {
        method: "POST",
        body: JSON.stringify({ token: startRes.token }),
      })
      .then((response) => response.json());
    if (!stopRes.ok || !Number.isInteger(stopRes.emitted) || stopRes.emitted < 1) {
      throw new Error(`stream stop failed: ${JSON.stringify(stopRes)}`);
    }
    const out = {
      target,
      iter,
      plannedEvents: startRes.total_events,
      injectedEvents: stopRes.emitted,
      ...sample,
      commitsPerEvent: Math.round((sample.commits / stopRes.emitted) * 100) / 100,
      taskDurationMs: (after.TaskDuration - before.TaskDuration) * 1000,
      scriptDurationMs: (after.ScriptDuration - before.ScriptDuration) * 1000,
      heapMB: await page.eval("(performance.memory?.usedJSHeapSize ?? 0) / 1048576"),
      cdp: metricsDelta(after, before),
      ...extra,
    };
    process.stdout.write(
      `stream ${target} commits=${sample.commits} task=${Math.round(out.taskDurationMs)}ms\n`,
    );
    return out;
  }

  if (SCENARIOS.includes("stream")) {
    const samples = [];
    await forEachSequential(indices(cyclesFor(5)), async (i) => {
      samples.push(await streamSample("conv_perf_a", i, { history: "window-only" }));
    });
    results.scenarios.stream = samples;
  }

  // ── Scenario: deep history (full page-in of sessions a and b) ──
  // The StickToBottom wrapper re-sticks programmatic scrollTop writes, so the
  // scroll-up must come from trusted wheel events (CDP Input) to escape the
  // lock and let HistoryAutoLoader chain-load pages.
  async function wheelScrollUp(cdp2, times = 1) {
    await forEachSequential(indices(times), () =>
      cdp2.send("Input.dispatchMouseEvent", {
        type: "mouseWheel",
        x: 800,
        y: 450,
        deltaX: 0,
        deltaY: -1200,
      }),
    );
  }
  async function pageInFullHistory(id, cdp2) {
    const marker = OLD_MARKERS[id];
    // Primary: the app's message deep-link — /c/{id}?message={oldestItem}
    // pages back until that item renders (app-native, fast).
    const oldestId = id === "conv_perf_a" ? "itm_a_00001" : "itm_b_00001";
    await cdp2.send("Page.navigate", { url: `${BASE}/c/${id}?message=${oldestId}` });
    async function waitForOldest(attempt) {
      if (attempt >= 90) return -1;
      if (attempt >= 30) await wheelScrollUp(cdp2, 3);
      await delay(attempt < 30 ? 1200 : 900);
      if (await page.eval(`(document.body?.textContent ?? "").includes(${JSON.stringify(marker)})`))
        return attempt + 1;
      return waitForOldest(attempt + 1);
    }
    return waitForOldest(0);
  }

  if (SCENARIOS.includes("deepHistory")) {
    const out = { load: [], stream: [], idleDeep: [] };
    await forEachSequential(["conv_perf_a", "conv_perf_b"], async (id) => {
      await gotoSession(id);
      await delay(1500);
      const loadT0 = Date.now();
      const attempts = await pageInFullHistory(id, cdp);
      const loadMs = Date.now() - loadT0;
      const probe = await page.eval(`(() => {
        const sc = document.querySelector('[data-virtualized-transcript]') ?? document.querySelector('[role="log"]');
        return { markerSeen: (document.body?.textContent ?? "").includes(${JSON.stringify(OLD_MARKERS[id])}), scrollHeight: sc?.scrollHeight ?? null };
      })()`);
      out.load.push({ target: id, loadMs, attempts, ...probe });
      process.stdout.write(
        `deepHistory load ${id}: ${loadMs}ms attempts=${attempts} seen=${probe.markerSeen} h=${probe.scrollHeight}\n`,
      );
      if (!probe.markerSeen) return;
      // The deep-link scroll position can be restored after a programmatic scroll.
      // Keep the loaded session mounted and wait until the latest marker is visible.
      async function waitForLatest(attempt) {
        if (attempt >= 20) return false;
        await page.eval(
          `(() => { const sc = document.querySelector('[data-virtualized-transcript]'); if (sc) sc.scrollTop = sc.scrollHeight; })()`,
        );
        await cdp.send("Input.dispatchMouseEvent", {
          type: "mouseWheel",
          x: 800,
          y: 450,
          deltaX: 0,
          deltaY: 12000,
        });
        await delay(1200);
        if (
          await page.eval(
            `(document.body?.textContent ?? "").includes(${JSON.stringify(MARKERS[id])})`,
          )
        )
          return true;
        return waitForLatest(attempt + 1);
      }
      const latestVisible = await waitForLatest(0);
      out.load.at(-1).latestVisible = latestVisible;
      if (!latestVisible) {
        process.stdout.write(`deepHistory latest marker not visible for ${id}; skipping stream\n`);
        return;
      }
      await delay(1500);
      const idleDeep = await idleSample(cdp);
      out.idleDeep.push({ target: id, ...idleDeep });
      process.stdout.write(
        `deepHistory idle ${id} task=${Math.round(idleDeep.taskDurationMs)}ms\n`,
      );

      // (ii) streaming into the deep session
      await forEachSequential(indices(cyclesFor(2)), async (i) => {
        const s = await streamSample(id, i, {
          history: "full",
          idleTaskMs: idleDeep.taskDurationMs,
        });
        s.taskMsPerEvent =
          Math.round(
            ((s.taskDurationMs - idleDeep.taskDurationMs) / Math.max(1, s.injectedEvents)) * 100,
          ) / 100;
        out.stream.push(s);
      });
    });
    results.scenarios.deepHistory = out;
  }

  results.consoleErrors = consoleErrors.slice(0, 50);
  const suffix = CPU_THROTTLE === 4 ? "-4x" : "";
  fs.writeFileSync(path.join(OUT, `results-v2${suffix}.json`), JSON.stringify(results, null, 1));
  console.log(`\nraw results → ${path.join(OUT, `results-v2${suffix}.json`)}`);
  clients.forEach((client) => client.close());
}

try {
  // Hard watchdog: a wedged run must never block the surrounding chain.
  const WATCHDOG_MS = Number(argOf("--watchdog-minutes", "25")) * 60_000;
  await Promise.race([
    main(),
    delay(WATCHDOG_MS, undefined, { ref: false }).then(() => {
      throw new Error(`watchdog: run exceeded ${WATCHDOG_MS / 60000} min`);
    }),
  ]);
} finally {
  await Promise.all(
    browsers.map(async ({ child, profile }) => {
      child.kill("SIGTERM");
      if (child.exitCode === null && child.signalCode === null) {
        await Promise.race([once(child, "exit"), delay(5000)]);
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
          await once(child, "exit");
        }
      }
      fs.rmSync(profile, { recursive: true, force: true });
    }),
  );
}
