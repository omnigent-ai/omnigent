// Per-switch phase split from the pf:* marks inside a collected trace.
export function analyzeTrace(traceEvents, mapOrig = () => null) {
  const marks = traceEvents
    .filter((e) => (e.cat ?? "").includes("blink.user_timing") && e.name?.startsWith("pf:"))
    .sort((a, b) => a.ts - b.ts);
  const clicks = marks.filter((m) => m.name === "pf:click");
  const out = [];
  for (let ci = 0; ci < clicks.length; ci++) {
    const clickTs = clicks[ci].ts;
    const nextClick = ci + 1 < clicks.length ? clicks[ci + 1].ts : Infinity;
    const inWin = marks.filter((m) => m.ts > clickTs && m.ts < nextClick);
    const painted = inWin.find((m) => m.name === "pf:painted");
    const settled = inWin.find((m) => m.name === "pf:settled");
    if (!painted) continue;
    const paintedTs = painted.ts;
    const settleTs = settled?.ts ?? paintedTs;
    const phases = { prePaint: 0, postPaint: 0 };
    const fnPre = new Map();
    const fnPost = new Map();
    const catsPre = {};
    const catsPost = {};
    let frames = 0;
    for (const e of traceEvents) {
      if (e.pid !== clicks[ci].pid || e.tid !== clicks[ci].tid) continue;
      const dur = e.dur ?? 0;
      if (
        ["DrawFrame", "Paint", "PrePaint"].includes(e.name) &&
        e.ts >= clickTs &&
        e.ts < paintedTs
      )
        frames++;
      if (dur <= 0) continue;
      const start = Math.max(e.ts, clickTs);
      const end = Math.min(e.ts + dur, nextClick, Math.max(settleTs, paintedTs));
      if (end <= start) continue;
      const pre = Math.max(0, Math.min(end, paintedTs) - start);
      const post = Math.max(0, end - Math.max(start, paintedTs));
      if (e.name === "RunTask") {
        phases.prePaint += pre;
        phases.postPaint += post;
      }
      if (["FunctionCall", "EventDispatch", "FireAnimationFrame", "TimerFire"].includes(e.name)) {
        const d = e.args?.data ?? {};
        const orig = d.url ? mapOrig(d.url, d.lineNumber ?? -1, d.columnNumber ?? 0) : null;
        const key = `${d.functionName || e.name}|${orig ?? (d.url ? d.url.replace(/^.*assets\//, "assets/") + ":" + ((d.lineNumber ?? -1) + 1) : "native")}`;
        if (pre) fnPre.set(key, (fnPre.get(key) ?? 0) + pre);
        if (post) fnPost.set(key, (fnPost.get(key) ?? 0) + post);
      } else if (
        /(GC|GCEvent|MinorGC|MajorGC)/.test(e.name) ||
        [
          "Layout",
          "UpdateLayoutTree",
          "ParseHTML",
          "Paint",
          "PrePaint",
          "Layerize",
          "RasterTask",
          "CommitLoad",
        ].includes(e.name) ||
        e.name.startsWith("v8.compile") ||
        e.name === "EvaluateScript"
      ) {
        if (pre) catsPre[e.name] = (catsPre[e.name] ?? 0) + pre;
        if (post) catsPost[e.name] = (catsPost[e.name] ?? 0) + post;
      }
    }
    const top = (m) =>
      [...m.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8)
        .map(([k, us]) => `${k} ${Math.round(us / 10) / 100}ms`);
    out.push({
      paintedAfterClickMs: Math.round((paintedTs - clickTs) / 10) / 100,
      settleAfterClickMs: Math.round((settleTs - clickTs) / 10) / 100,
      framesBeforePaint: frames,
      prePaintMs: Math.round(phases.prePaint / 10) / 100,
      postPaintMs: Math.round(phases.postPaint / 10) / 100,
      categoriesPre: Object.fromEntries(
        Object.entries(catsPre).map(([k, v]) => [k, Math.round(v / 10) / 100]),
      ),
      categoriesPost: Object.fromEntries(
        Object.entries(catsPost).map(([k, v]) => [k, Math.round(v / 10) / 100]),
      ),
      topPre: top(fnPre),
      topPost: top(fnPost),
    });
  }
  return out;
}
