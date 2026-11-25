// Deterministic fixture generator for the web performance baseline.
//
//   node web/perf/fixture-gen.mjs
//
// Seeded content uses generic examples; timestamps are relative to generation.

import { mkdirSync, writeFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "fixtures");

// ── Seeded PRNG ──────────────────────────────────────────────
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function makeRng(seed) {
  const next = mulberry32(seed);
  return {
    next,
    int: (lo, hi) => lo + Math.floor(next() * (hi - lo + 1)),
    pick: (arr) => arr[Math.floor(next() * arr.length)],
    chance: (p) => next() < p,
    shuffle(arr) {
      for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(next() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      return arr;
    },
  };
}

// ── Word bank (generic, example.com-style data) ──────────────
const WORDS = (
  "queue widget handler pipeline snapshot parser session payload buffer ticker" +
  " renderer debounce throttle observable reducer selector middleware cache" +
  " fixture schema migration endpoint retry token ledger manifest vector" +
  " sampler profiler telemetry request response batch worker channel promise" +
  " scheduler interval lattice segment fragment overlay divider marquee anchor" +
  " stepper progress spinner toast dialog drawer canvas raster viewport"
).split(" ");

const SUBJECTS = [
  "Refactor the ingestion pipeline",
  "Fix flaky pagination test",
  "Add retry backoff to the sync worker",
  "Profile the transcript renderer",
  "Split the settings store",
  "Migrate legacy list markup",
  "Batch the events endpoint",
  "Wire the scheduler tick",
  "Trim the startup bundle",
  "Rewrite the changelog generator",
];

const SENTENCE_TAILS = [
  "so the next run stays deterministic",
  "which keeps the render path allocation-free",
  "and the snapshot test covers the edge case",
  "without regressing the warm-start budget",
  "while keeping the public API unchanged",
  "then the error path degrades gracefully",
];

const LANGS = [
  "typescript",
  "tsx",
  "python",
  "rust",
  "go",
  "json",
  "bash",
  "sql",
  "yaml",
  "css",
  "java",
  "diff",
];

function sentence(rng) {
  const n = rng.int(6, 14);
  const words = [];
  for (let i = 0; i < n; i++) words.push(rng.pick(WORDS));
  const tail = rng.pick(SENTENCE_TAILS);
  const s = words.join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1) + " " + tail + ".";
}

function paragraph(rng, sentences = 4) {
  const parts = [];
  for (let i = 0; i < sentences; i++) parts.push(sentence(rng));
  return parts.join(" ");
}

function codeSnippet(rng, lang, lines) {
  const body = [];
  for (let i = 0; i < lines; i++) {
    const w1 = rng.pick(WORDS);
    const w2 = rng.pick(WORDS);
    switch (lang) {
      case "typescript":
        body.push(`export function ${w1}${i}(${w2}: string): Promise<void> {`);
        body.push(`  const payload = await load${w1.charAt(0).toUpperCase() + w1.slice(1)}(${w2});`);
        body.push(`  metrics.record("${w1}", payload.size);`);
        body.push("}");
        break;
      case "tsx":
        body.push(`export const ${w1.charAt(0).toUpperCase() + w1.slice(1)}Row = memo(function ${w1}${i}({ item }: Props) {`);
        body.push(`  const [open, setOpen] = useState(false);`);
        body.push(`  return <li onClick={() => setOpen(!open)}>{item.${w2}}</li>;`);
        body.push("});");
        break;
      case "python":
        body.push(`def ${w1}_step_${i}(${w2}: dict[str, Any]) -> StepResult:`);
        body.push(`    payload = load_${w1}(${w2}["id"])`);
        body.push(`    metrics.record("${w1}", len(payload))`);
        body.push(`    return StepResult(ok=True, name="${w1}")`);
        break;
      case "rust":
        body.push(`fn ${w1}_pass_${i}(&self, ${w2}: &mut Vec<u8>) -> Result<usize, Error> {`);
        body.push(`    let mut written = 0usize;`);
        body.push(`    for chunk in self.${w2}s.drain(..) { written += chunk.len(); }`);
        body.push(`    Ok(written)`);
        body.push(`}`);
        break;
      case "go":
        body.push(`func (s *${w1}Store) step${i}(ctx context.Context, ${w2} string) error {`);
        body.push(`    payload, err := s.load(ctx, ${w2})`);
        body.push(`    if err != nil { return fmt.Errorf("${w1}: %w", err) }`);
        body.push(`    return s.commit(ctx, payload)`);
        body.push(`}`);
        break;
      case "json":
        body.push(`  {`);
        body.push(`    "id": "obj_${rng.int(1000, 9999)}",`);
        body.push(`    "kind": "${w1}",`);
        body.push(`    "count": ${rng.int(0, 500)},`);
        body.push(`    "tags": ["${w2}", "${rng.pick(WORDS)}"]`);
        body.push(`  },`);
        break;
      case "bash":
        body.push(`set -euo pipefail`);
        body.push(`for f in out/${w1}-*.log; do`);
        body.push(`  gzip -c "$f" > "$f.gz" && rm "$f"`);
        body.push(`done`);
        break;
      case "sql":
        body.push(`SELECT ${w1}_id, count(*) AS hits`);
        body.push(`FROM ${w2}_events_${i}`);
        body.push(`WHERE created_at > now() - interval '7 days'`);
        body.push(`GROUP BY ${w1}_id ORDER BY hits DESC LIMIT 20;`);
        break;
      case "yaml":
        body.push(`${w1}:`);
        body.push(`  image: registry.example.com/app:${rng.int(1, 99)}`);
        body.push(`  ${w2}:`);
        body.push(`    - name: run-${i}`);
        body.push(`      command: ["./scripts/${w1}.sh"]`);
        break;
      case "css":
        body.push(`.${w1}-panel-${i} {`);
        body.push(`  display: grid;`);
        body.push(`  grid-template-columns: minmax(0, 1fr) 320px;`);
        body.push(`  --${w2}-offset: ${rng.int(0, 96)}px;`);
        body.push(`}`);
        break;
      case "java":
        body.push(`public class ${w1.charAt(0).toUpperCase() + w1.slice(1)}Service${i} {`);
        body.push(`  public Result handle(Request ${w2}) {`);
        body.push(`    return registry.lookup(${w2}.id()).map(Result::ok).orElseThrow();`);
        body.push(`  }`);
        body.push(`}`);
        break;
      case "diff":
        body.push(`--- a/src/${w1}.ts`);
        body.push(`+++ b/src/${w1}.ts`);
        body.push(`@@ -${rng.int(1, 200)},7 +${rng.int(1, 200)},8 @@`);
        body.push(`-  return legacy${i}(${w2});`);
        body.push(`+  return await ${w1}Adapter.run(${w2});`);
        body.push(`+  metrics.tick("${w1}");`);
        break;
    }
  }
  return body.join("\n");
}

function mdMessage(rng, { big = false } = {}) {
  const parts = [];
  const headings = big ? rng.int(3, 5) : rng.int(1, 2);
  for (let h = 0; h < headings; h++) {
    parts.push(`## ${rng.pick(SUBJECTS)} — step ${h + 1}`);
    parts.push(paragraph(rng, big ? rng.int(3, 6) : 2));
    if (rng.chance(0.5)) {
      const items = rng.int(3, big ? 10 : 5);
      for (let i = 0; i < items; i++) parts.push(`- ${sentence(rng)}`);
    }
  }
  if (rng.chance(0.35)) {
    parts.push("| name | status | count |");
    parts.push("|------|--------|-------|");
    const rows = rng.int(3, big ? 14 : 5);
    for (let i = 0; i < rows; i++) {
      parts.push(`| ${rng.pick(WORDS)} | ${rng.pick(["ok", "pending", "failed"])} | ${rng.int(0, 999)} |`);
    }
  }
  const blocks = big ? rng.int(3, 6) : rng.int(1, 2);
  for (let b = 0; b < blocks; b++) {
    const lang = rng.pick(LANGS);
    parts.push(`\`\`\`${lang}`);
    parts.push(codeSnippet(rng, lang, rng.int(6, big ? 26 : 12)));
    parts.push("```");
  }
  if (big && rng.chance(0.4)) parts.push(`> ${paragraph(rng, 2)}`);
  return parts.join("\n\n");
}

function bigOutput(rng) {
  const kind = rng.int(0, 2);
  const lines = [];
  if (kind === 0) {
    // JSON dump
    for (let i = 0; i < rng.int(60, 160); i++) {
      lines.push(
        `{"id":"row_${i.toString().padStart(4, "0")}","name":"${rng.pick(WORDS)}_${i}","size":${rng.int(1, 9999)},"ok":${rng.chance(0.9)}}`,
      );
    }
    return lines.join("\n");
  }
  if (kind === 1) {
    // Log stream
    for (let i = 0; i < rng.int(80, 220); i++) {
      lines.push(
        `${String(rng.int(0, 23)).padStart(2, "0")}:${String(rng.int(0, 59)).padStart(2, "0")}:${String(rng.int(0, 59)).padStart(2, "0")} ${rng.pick(["INFO", "DEBUG", "WARN"])} ${rng.pick(WORDS)}.${rng.pick(WORDS)} step=${i} status=${rng.pick(["ok", "retry", "skip"])}`,
      );
    }
    return lines.join("\n");
  }
  // Stack trace
  lines.push(`Traceback (most recent call last):`);
  for (let i = 0; i < rng.int(10, 24); i++) {
    lines.push(
      `  File "/home/dev/example-project/src/${rng.pick(WORDS)}/mod_${i}.py", line ${rng.int(1, 900)}, in ${rng.pick(WORDS)}`,
    );
  }
  lines.push(`${rng.pick(["TimeoutError", "ConnectionResetError", "ValueError"])}: ${sentence(rng)}`);
  return lines.join("\n");
}

const TOOL_NAMES = ["read_file", "list_directory", "run_command", "search_codebase", "edit_file"];

function toolCall(rng, sessionId, seq) {
  const name = rng.pick(TOOL_NAMES);
  const args = {
    read_file: { path: `src/${rng.pick(WORDS)}/mod_${seq}.ts`, start_line: 1, end_line: 240 },
    list_directory: { path: `src/${rng.pick(WORDS)}`, recursive: rng.chance(0.4) },
    run_command: { command: `pnpm ${rng.pick(["test", "lint", "build"])} --filter web` },
    search_codebase: { query: `${rng.pick(WORDS)} ${rng.pick(WORDS)}`, limit: 50 },
    edit_file: {
      path: `src/${rng.pick(WORDS)}/mod_${seq}.ts`,
      old_string: `return legacy${seq}(payload);`,
      new_string: `return await adapter.run(payload);`,
    },
  }[name];
  return {
    id: `itm_${sessionId}_${String(seq).padStart(5, "0")}`,
    type: "function_call",
    response_id: "",
    status: "completed",
    name,
    arguments: JSON.stringify(args),
    call_id: `call_${sessionId}_${seq}`,
    created_at: 0,
  };
}

// ── Session generator ────────────────────────────────────────

function genSession(spec, seedBase) {
  const rng = makeRng(seedBase);
  const items = [];
  let seq = 0;
  let clock = spec.created_at;
  const turnCount = Math.floor(spec.item_count / 4);
  for (let t = 0; t < turnCount; t++) {
    clock += rng.int(30, 600);
    const rid = `resp_${spec.key}_${String(t).padStart(4, "0")}`;
    const baseItem = () => ({
      id: `itm_${spec.key}_${String(++seq).padStart(5, "0")}`,
      response_id: rid,
      status: "completed",
      created_at: clock,
    });

    // 1. user message (the first turn's user message also carries the
    // deep-history marker so the driver can detect "all pages loaded").
    const isFirstTurn = t === 0;
    items.push({
      ...baseItem(),
      type: "message",
      role: "user",
      content: [
        {
          type: "input_text",
          text: `${isFirstTurn ? `${oldestMarker(spec.key)} ` : ""}${rng.pick(SUBJECTS)}? ${sentence(rng)} ${rng.chance(0.3) ? paragraph(rng, 2) : ""}`.trim(),
        },
      ],
      created_by: "dev@example.com",
    });

    // 2. reasoning (sometimes)
    if (rng.chance(0.25)) {
      items.push({
        ...baseItem(),
        type: "reasoning",
        model: "example-model",
        summary: [{ type: "summary_text", text: paragraph(rng, 2) }],
        content: [{ type: "reasoning_text", text: paragraph(rng, rng.int(2, 5)) }],
      });
    }

    // 3. routing decision (display-only chip)
    if (rng.chance(0.3)) {
      items.push({
        ...baseItem(),
        type: "routing_decision",
        model: `example-router-model-${rng.int(1, 3)}`,
        applied: rng.chance(0.7),
        rationale: sentence(rng),
      });
    }

    // 4. assistant markdown message (sometimes split into commentary+final)
    const big = rng.chance(spec.big_share);
    items.push({
      ...baseItem(),
      type: "message",
      role: "assistant",
      content: [{ type: "output_text", text: mdMessage(rng, { big }) }],
      model: "example-model",
    });
    if (rng.chance(0.25)) {
      items.push({
        ...baseItem(),
        type: "message",
        role: "assistant",
        content: [{ type: "output_text", text: paragraph(rng, 2) }],
        model: "example-model",
        phase: "final_answer",
      });
    }

    // 5. tool call + output
    if (rng.chance(0.6)) {
      const call = toolCall(rng, spec.key, ++seq);
      items.push(call);
      const outputBig = rng.chance(spec.big_tool_share);
      items.push({
        ...baseItem(),
        type: "function_call_output",
        call_id: call.call_id,
        output: outputBig ? bigOutput(rng) : paragraph(rng, 3),
      });
    }

    // 6. occasional terminal command pair / slash command
    if (rng.chance(0.08)) {
      const cmd = `pnpm ${rng.pick(WORDS)}:${rng.pick(WORDS)}`;
      items.push({
        ...baseItem(),
        type: "terminal_command",
        kind: "input",
        input: cmd,
      });
      items.push({
        ...baseItem(),
        type: "terminal_command",
        kind: "output",
        stdout: Array.from({ length: rng.int(5, 30) }, () => sentence(rng)).join("\n"),
      });
    }
    if (rng.chance(0.05)) {
      items.push({
        ...baseItem(),
        type: "slash_command",
        kind: "skill",
        name: `example-${rng.pick(WORDS)}`,
        arguments: "--scope=example",
        output: paragraph(rng, 2),
        model: "example-model",
      });
    }
  }

  // Stamp response ids onto everything in the turn groups (assistant/etc. already carry them).
  return {
    session: {
      id: spec.id,
      object: "conversation",
      title: spec.title,
      created_at: spec.created_at,
      updated_at: spec.updated_at,
      labels: { perf: spec.key },
      permission_level: null,
      owner: "dev@example.com",
      agent_id: "ag_perf",
      agent_name: "example-agent",
      harness: "example",
      workspace: "/home/dev/example-project",
      status: "idle",
      runner_id: "runner_perf",
      runner_online: true,
      host_id: null,
      host_online: null,
      archived: false,
      pending_elicitations_count: 0,
    },
    items,
  };
}

// ── Workspace fixtures (shared tree + changes) ───────────────

function genTree(rng, totalEntries) {
  // Build a deep, wide directory tree from synthetic path segments.
  const topDirs = Array.from({ length: 8 }, (_, i) => `${WORDS[(i * 7) % WORDS.length]}_${i}`);
  const entries = [];
  let made = 0;
  let i = 0;
  while (made < totalEntries) {
    const depth = rng.int(1, 6);
    const segs = [rng.pick(topDirs)];
    for (let d = 1; d < depth; d++) segs.push(`${rng.pick(WORDS)}${rng.int(0, 9)}`);
    const dir = segs.join("/");
    // A directory row plus a few files under it.
    entries.push({ name: segs[segs.length - 1], path: dir, type: "ENTRY_TYPE_DIRECTORY", ignored: false });
    made++;
    const files = Math.min(rng.int(2, 5), totalEntries - made);
    for (let f = 0; f < files; f++) {
      const ext = rng.pick(["ts", "tsx", "py", "md", "json", "css", "sql"]);
      const name = `${rng.pick(WORDS)}_${made.toString().padStart(5, "0")}.${ext}`;
      entries.push({
        name,
        path: `${dir}/${name}`,
        type: "ENTRY_TYPE_FILE",
        size: rng.int(200, 90000),
        ignored: rng.chance(0.02),
      });
      made++;
    }
    i++;
  }
  return entries;
}

function genChanges(rng, count) {
  const changes = [];
  for (let i = 0; i < count; i++) {
    const dir = `${WORDS[(i * 5) % WORDS.length]}_${i % 17}`;
    const name = `${rng.pick(WORDS)}_${i}.${rng.pick(["ts", "tsx", "py", "css", "md"])}`;
    const additions = rng.int(0, 240);
    const deletions = rng.int(0, 180);
    changes.push({
      path: `src/${dir}/${name}`,
      change_type: additions === 0 ? "CHANGE_TYPE_DELETED" : deletions === 0 ? "CHANGE_TYPE_ADDED" : "CHANGE_TYPE_MODIFIED",
      additions: String(additions),
      deletions: String(deletions),
      binary: false,
    });
  }
  return changes;
}

function genFileContent(rng, lines) {
  const out = [];
  for (let i = 0; i < lines; i++) {
    const w1 = rng.pick(WORDS);
    const w2 = rng.pick(WORDS);
    out.push(
      `// ${String(i + 1).padStart(4, "0")} — ${w1} ${w2} handler, keeps the ${rng.pick(WORDS)} bounded and retries transient failures.`,
    );
    out.push(`export async function ${w1}${i}(input: ${w1.charAt(0).toUpperCase() + w1.slice(1)}Input) {`);
    out.push(`  const step = await queue.push("${w1}", input);`);
    out.push(`  return { ok: true, id: step.id };`);
    out.push(`}`);
  }
  return out.join("\n");
}

// Marker planted in each session's OLDEST user message so the driver can
// verify that the full history (all pages) has been loaded.
function oldestMarker(key) {
  return `SESSOLD-${key.toUpperCase()}`;
}

// ── Main ─────────────────────────────────────────────────────

const SESSION_SPECS = [
  { key: "a", item_count: 500, big_share: 0.18, big_tool_share: 0.15 },
  { key: "b", item_count: 400, big_share: 0.12, big_tool_share: 0.1 },
  { key: "c", item_count: 360, big_share: 0.12, big_tool_share: 0.08 },
  { key: "d", item_count: 320, big_share: 0.1, big_tool_share: 0.12 },
  { key: "e", item_count: 300, big_share: 0.1, big_tool_share: 0.08 },
];

const TITLES = {
  a: "Perf A — extreme streaming replay",
  b: "Perf B — docs rewrite",
  c: "Perf C — refactor pass",
  d: "Perf D — data pipeline",
  e: "Perf E — migration",
};

function main() {
  mkdirSync(OUT_DIR, { recursive: true });
  const now = Math.floor(Date.now() / 1000);
  const list = [];
  const stats = [];

  SESSION_SPECS.forEach((spec, idx) => {
    const id = `conv_perf_${spec.key}`;
    const { session, items } = genSession(
      { ...spec, id, key: spec.key, title: TITLES[spec.key], created_at: now - 86400 * (idx + 1), updated_at: now - 3600 * (idx + 1) },
      0x5eed0000 + idx * 7919,
    );
    writeFileSync(path.join(OUT_DIR, `items_${spec.key}.json`), JSON.stringify(items));
    const bytes = items.reduce((acc, it) => {
      if (it.type === "message") return acc + (it.content?.[0]?.text?.length ?? 0);
      if (it.type === "function_call_output") return acc + it.output.length;
      if (it.type === "reasoning") return acc + (it.content?.[0]?.text?.length ?? 0);
      return acc + 200;
    }, 0);
    const codeBlocks = items.reduce(
      (acc, it) => acc + (it.type === "message" ? (it.content?.[0]?.text?.match(/```/g)?.length ?? 0) / 2 : 0),
      0,
    );
    const toolCalls = items.filter((it) => it.type === "function_call").length;
    stats.push({
      id,
      items: items.length,
      approx_chars: bytes,
      code_blocks: Math.round(codeBlocks),
      tool_calls: toolCalls,
      reasoning: items.filter((it) => it.type === "reasoning").length,
    });
    list.push(session);
  });

  // Filler sessions keep the sidebar modest (virtualization is a separate effort).
  for (let f = 0; f < 15; f++) {
    const id = `conv_perf_f${String(f + 1).padStart(2, "0")}`;
    list.push({
      id,
      object: "conversation",
      title: `Filler session ${f + 1}`,
      created_at: now - 86400 * 2 - f * 600,
      updated_at: now - 7200 - f * 600,
      labels: {},
      permission_level: null,
      owner: "dev@example.com",
      agent_id: "ag_perf",
      agent_name: "example-agent",
      harness: "example",
      status: "idle",
      runner_online: false,
      archived: false,
      pending_elicitations_count: 0,
    });
    writeFileSync(
      path.join(OUT_DIR, `items_${id}.json`),
      JSON.stringify(
        Array.from({ length: 8 }, (_, i) => ({
          id: `itm_${id}_${String(i).padStart(3, "0")}`,
          type: "message",
          response_id: `resp_${id}_${i}`,
          status: "completed",
          role: i % 2 === 0 ? "user" : "assistant",
          content: [{ type: i % 2 === 0 ? "input_text" : "output_text", text: `Filler message ${i + 1} of session ${f + 1}. ${sentence(makeRng(1000 + f * 10 + i))}` }],
          created_at: now - 7200 - f * 600 + i * 30,
        })),
      ),
    );
  }

  list.sort((x, y) => y.updated_at - x.updated_at);
  writeFileSync(path.join(OUT_DIR, "sessions.json"), JSON.stringify(list, null, 1));

  const treeRng = makeRng(0x7ee1);
  const tree = genTree(treeRng, 3000);
  writeFileSync(path.join(OUT_DIR, "tree.json"), JSON.stringify(tree));

  const changesRng = makeRng(0xd1ff);
  const changes = genChanges(changesRng, 120);
  writeFileSync(path.join(OUT_DIR, "changes.json"), JSON.stringify(changes));

  const fileRng = makeRng(0xf11e);
  const bigFile = genFileContent(fileRng, 1400);
  writeFileSync(path.join(OUT_DIR, "bigfile.txt"), bigFile);
  const hugeRng = makeRng(0xf22e);
  const hugeFile = genFileContent(hugeRng, 5000);
  writeFileSync(path.join(OUT_DIR, "hugefile.txt"), hugeFile);

  // Report
  const totals = stats.reduce((acc, s) => ({ items: acc.items + s.items, chars: acc.chars + s.approx_chars }), { items: 0, chars: 0 });
  const treeBytes = statSync(path.join(OUT_DIR, "tree.json")).size;
  console.log("Fixture generation complete →", OUT_DIR);
  for (const s of stats) {
    console.log(
      `  ${s.id}: ${s.items} items, ~${(s.approx_chars / 1024).toFixed(0)} KB text, ${s.code_blocks} code blocks, ${s.tool_calls} tool calls, ${s.reasoning} reasoning`,
    );
  }
  console.log(
    `  Heavy total: ${totals.items} items, ~${(totals.chars / 1024 / 1024).toFixed(2)} MB text; tree.json ${(treeBytes / 1024).toFixed(0)} KB (${tree.length} entries); changes: ${changes.length} files`,
  );
}

main();
