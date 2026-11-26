// Interleaved, paired switch comparisons against two independently served builds.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { ensurePortFree, forEachSequential, waitHttp } from "./cdp.mjs";
import { withLifecycle } from "./lifecycle.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");

export function percentile(values, p) {
  if (
    !values.length ||
    !Number.isFinite(p) ||
    p < 0 ||
    p > 100 ||
    values.some((value) => !Number.isFinite(value))
  ) {
    throw new Error("percentile requires finite values and a percentile from 0 to 100");
  }
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil((p / 100) * sorted.length) - 1)];
}

export function bootstrapDelta(a, b, p, iterations = 5000, random = Math.random) {
  if (!a.length || a.length !== b.length || iterations < 1)
    throw new Error("bootstrap requires equal nonempty paired samples");
  const deltas = [];
  for (let iteration = 0; iteration < iterations; iteration++) {
    const left = [],
      right = [];
    for (let i = 0; i < a.length; i++) {
      const index = Math.floor(random() * a.length);
      left.push(a[index]);
      right.push(b[index]);
    }
    deltas.push(percentile(right, p) - percentile(left, p));
  }
  return [percentile(deltas, 2.5), percentile(deltas, 97.5)];
}

function compare(a, b) {
  return Object.fromEntries(
    [50, 75, 90].map((p) => {
      const baseline = percentile(a, p);
      const candidate = percentile(b, p);
      return [
        `p${p}`,
        { a: baseline, b: candidate, delta: candidate - baseline, ci95: bootstrapDelta(a, b, p) },
      ];
    }),
  );
}

async function main() {
  return withLifecycle(async (scope) => {
    async function run(command, args, cwd) {
      const child = scope.spawn(command, args, { cwd, stdio: "inherit" });
      await scope.wait(child);
    }
    const args = process.argv.slice(2);
    const arg = (name, fallback) => {
      const index = args.indexOf(name);
      return index < 0 ? fallback : args[index + 1];
    };
    const output = path.resolve(arg("--out", path.join(here, "out", "ab")));
    const samples = Number(arg("--samples", "30"));
    const throttle = Number(arg("--throttle", "1"));
    if (!Number.isInteger(samples) || samples < 1 || !Number.isFinite(throttle) || throttle < 1)
      throw new Error("invalid sample count or throttle");
    fs.mkdirSync(output, { recursive: true });
    if (!fs.existsSync(path.join(here, "fixtures", "sessions.json")))
      await run(process.execPath, [path.join(here, "fixture-gen.mjs")], repo);
    async function build(arm) {
      const dist = arg(`--${arm}-dist`);
      if (dist) return path.resolve(dist);
      const source = arg(`--${arm}`, repo);
      let worktree = path.resolve(source);
      if (!fs.existsSync(path.join(worktree, "web", "vite.config.ts"))) {
        worktree = fs.mkdtempSync(path.join(output, `checkout-${arm}-`));
        fs.rmdirSync(worktree);
        const temporary = worktree;
        scope.cleanup(() => {
          const listed = spawnSync("git", ["worktree", "list", "--porcelain"], {
            cwd: repo,
            encoding: "utf8",
          });
          if (listed.error || listed.status !== 0)
            throw listed.error ?? new Error(`git worktree list failed: ${listed.status}`);
          if (listed.stdout.split("\n").includes(`worktree ${temporary}`)) {
            const removed = spawnSync("git", ["worktree", "remove", "--force", temporary], {
              cwd: repo,
              stdio: "inherit",
            });
            if (removed.error || removed.status !== 0)
              throw removed.error ?? new Error(`git worktree remove failed: ${removed.status}`);
          } else fs.rmSync(temporary, { recursive: true, force: true });
        });
        await run("git", ["worktree", "add", "--detach", worktree, source], repo);
        await run("pnpm", ["install", "--frozen-lockfile", "--filter", "web"], worktree);
      }
      const outDir = path.join(output, `dist-${arm}`);
      await run(
        "pnpm",
        [
          "exec",
          "vite",
          "build",
          "--config",
          "vite.config.ts",
          "--outDir",
          outDir,
          "--emptyOutDir",
        ],
        path.join(worktree, "web"),
      );
      return outDir;
    }
    const dists = [await build("a"), await build("b")];
    for (const dist of dists)
      if (!fs.existsSync(path.join(dist, "index.html")))
        throw new Error(`dist has no index.html: ${dist}`);
    const ports = [Number(arg("--port-a", "5299")), Number(arg("--port-b", "5300"))];
    const cdpPort = Number(arg("--cdp-port", "9336"));
    const cdpPortB = Number(arg("--cdp-port-b", "9337"));
    if (new Set([...ports, cdpPort, cdpPortB]).size !== 4)
      throw new Error("ports must be distinct");
    await Promise.all([...ports, cdpPort, cdpPortB].map(ensurePortFree));
    await forEachSequential(ports, async (_, i) => {
      const serverOut = path.join(output, `server-${i}`);
      fs.mkdirSync(serverOut, { recursive: true });
      const log = fs.openSync(path.join(serverOut, "server.log"), "w");
      const child = scope.spawn(
        process.execPath,
        [
          path.join(here, "server.mjs"),
          "--port",
          String(ports[i]),
          "--dist",
          dists[i],
          "--out",
          serverOut,
          "--latency-ms",
          arg("--latency-ms", "0"),
          "--features",
          arg(i ? "--features-b" : "--features-a", ""),
        ],
        { stdio: ["ignore", log, log] },
      );
      fs.closeSync(log);
      await waitHttp(`http://127.0.0.1:${ports[i]}/__perf__/counters`, scope.signal);
      if (child.exitCode !== null) throw new Error(`mock server exited: ${child.exitCode}`);
    });
    const benchArgs = [
      path.join(here, "bench.mjs"),
      "--url",
      `http://127.0.0.1:${ports[0]}`,
      "--ab-url",
      `http://127.0.0.1:${ports[1]}`,
      "--scenarios",
      "ab",
      "--samples",
      String(samples),
      "--throttle",
      String(throttle),
      "--cdp-port",
      String(cdpPort),
      "--cdp-port-b",
      String(cdpPortB),
      "--out",
      output,
    ];
    if (arg("--chrome")) benchArgs.push("--chrome", arg("--chrome"));
    const bench = scope.spawn(process.execPath, benchArgs, { stdio: "inherit" }, 15000);
    await scope.wait(bench);
    const rawFile = path.join(output, throttle === 4 ? "results-v2-4x.json" : "results-v2.json");
    const raw = JSON.parse(fs.readFileSync(rawFile, "utf8"));
    const { a, b } = raw.scenarios.ab;
    const fields = {
      clickToPaintedMs: (row) => row.clickToPaintedMs,
      clickToWorkDoneMs: (row) => row.clickToWorkDoneMs,
      commits: (row) => row.commits,
      commitsToPaint: (row) => row.commitsToPaint,
      commitsPointerToClick: (row) => row.commitsPointerToClick,
      longTasks: (row) => row.busyDuringSwitch.count,
      layoutMs: (row) => row.cdp.LayoutDuration * 1000,
      recalcStyleMs: (row) => row.cdp.RecalcStyleDuration * 1000,
      heapMB: (row) => row.heapMB,
    };
    const summary = {
      samples,
      throttle,
      dists,
      discardedPairs: raw.discardedPairs,
      placeholder: Object.fromEntries(
        [
          ["a", a],
          ["b", b],
        ].map(([arm, rows]) => [
          arm,
          {
            mounted: rows.filter((row) => row.placeholder.mounted).length,
            painted: rows.filter((row) => row.placeholder.painted).length,
          },
        ]),
      ),
      comparison: {},
      orderPosition: {},
    };
    for (const [name, field] of Object.entries(fields))
      summary.comparison[name] = compare(a.map(field), b.map(field));
    for (const [name, parity] of [
      ["aFirst", 0],
      ["bFirst", 1],
    ]) {
      const left = a.filter((_, index) => index % 2 === parity);
      const right = b.filter((_, index) => index % 2 === parity);
      if (!left.length) continue;
      summary.orderPosition[name] = { n: left.length };
      for (const metric of ["clickToPaintedMs", "clickToWorkDoneMs", "heapMB"]) {
        const field = fields[metric];
        summary.orderPosition[name][metric] = compare(left.map(field), right.map(field));
      }
    }
    fs.writeFileSync(path.join(output, "ab-summary.json"), JSON.stringify(summary, null, 2));
    console.log(JSON.stringify(summary, null, 2));
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href)
  await main();
