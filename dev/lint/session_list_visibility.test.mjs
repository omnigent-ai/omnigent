import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { analyzeSource } from "./session_list_visibility.mjs";

const lint = (source) => analyzeSource("example.ts", source);

for (const [name, source] of Object.entries({
  "literal URL": `fetch("/v1/sessions?visibility=all")`,
  "encoded query key": `fetch("/v1/sessions?%76isibility=mine")`,
  "parameter constructor": `const params = new URLSearchParams({ limit: "25", visibility: visibility ?? "all" });
    authenticatedFetch(\`/v1/sessions?\${params.toString()}\`);`,
  "nonempty constructor alternatives": `const params = new URLSearchParams({ visibility: flag ? "all" : "mine" }); fetch("/v1/sessions?" + params);`,
  "nonempty set alternatives": `const params = new URLSearchParams(); params.set("visibility", flag ? "mine" : "shared"); fetch("/v1/sessions?" + params);`,
  "parameter set": `const params = new URLSearchParams(); params.set("visibility", "all");
    hostFetch(base + "/v1/sessions?" + params);`,
  "parameter append": `const params = new URLSearchParams(); params.append("visibility", scope);
    fetch("/v1/sessions?" + params.toString());`,
  "helper return": `function sessionListQuery(limit) {
    const query = new URLSearchParams({ limit }); query.set("visibility", "all"); return query.toString();
    } authenticatedFetch(\`/v1/sessions?\${sessionListQuery(25)}\`);`,
  "arrow helper": `const query = (visibility) => new URLSearchParams({ visibility }).toString();
    fetch("/v1/sessions?" + query("all"));`,
  "helper argument": `const query = (params) => params.toString();
    fetch("/v1/sessions?" + query(new URLSearchParams({ visibility: "all" })));`,
  "request config": `client.request({ url: "/v1/sessions", method: "GET", params: { visibility: "all" } });`,
  "get config": `client.get("/v1/sessions", { searchParams: new URLSearchParams({ visibility: "all" }) });`,
  POST: `authenticatedFetch("/v1/sessions", { method: "POST", body: data });`,
  "resolved POST options": `const options = { method: "POST" }; fetch("/v1/sessions", options);`,
  "POST Request": `const request = new Request("/v1/sessions", { method: "POST" }); fetch(request);`,
  "single session": `fetch(\`/v1/sessions/\${id}\`); client.get("/v1/sessions/id");`,
  "child sessions and projects": `fetch("/v1/sessions/id/child_sessions"); fetch("/v1/sessions/projects");`,
  "list endpoint inside another endpoint's query": `fetch("/v1/projects?redirect=/v1/sessions");`,
  "comments and strings": `// fetch("/v1/sessions")
    const example = 'fetch("/v1/sessions")';`,
  "inline suppression": `fetch("/v1/sessions"); // custom-lint: disable=session-list-visibility -- legacy server`,
  "next-line suppression": `// custom-lint: disable-next=another-rule, session-list-visibility -- intentional
    fetch("/v1/sessions");`,
  "block comment suppression": `/* custom-lint: disable-next=session-list-visibility */
    fetch("/v1/sessions");`,
})) {
  test(`accepts ${name}`, () => assert.deepEqual(lint(source), []));
}

for (const [name, source] of Object.entries({
  "bare list URL": `fetch("/v1/sessions");`,
  "missing parameter": `authenticatedFetch("/v1/sessions?limit=25");`,
  "empty visibility": `fetch("/v1/sessions?visibility=");`,
  "empty parameter constructor": `const params = new URLSearchParams({ visibility: "" }); fetch("/v1/sessions?" + params);`,
  "conditionally empty parameter constructor": `const params = new URLSearchParams({ visibility: flag ? "all" : "" }); fetch("/v1/sessions?" + params);`,
  "empty parameter set": `const params = new URLSearchParams(); params.set("visibility", ""); fetch("/v1/sessions?" + params);`,
  "conditionally empty parameter set": `const params = new URLSearchParams({ visibility: "all" }); params.set("visibility", flag ? "mine" : ""); fetch("/v1/sessions?" + params);`,
  "empty request params": `client.get("/v1/sessions", { params: { visibility: "" } });`,
  "concatenated URL": `hostFetch(base + "/v1/sessions?limit=25");`,
  "get config without visibility": `client.get("/v1/sessions", { params: { limit: 25 } });`,
  "request config without visibility": `client.request({ url: "/v1/sessions", method: "GET" });`,
  "unsupported query helper": `fetch(\`/v1/sessions?\${externalQuery()}\`);`,
  "unrelated visibility": `const params = new URLSearchParams({ visibility: "all" });
    fetch("/v1/sessions?limit=25");`,
  "unrelated parameter binding": `const other = new URLSearchParams({ visibility: "all" });
    const params = new URLSearchParams(); fetch("/v1/sessions?" + params.toString());`,
  "shadowed parameter binding": `const params = new URLSearchParams({ visibility: "all" });
    function load() { const params = new URLSearchParams(); fetch("/v1/sessions?" + params); }`,
  "conditional set": `const params = new URLSearchParams();
    if (visibility) params.set("visibility", visibility); fetch("/v1/sessions?" + params);`,
  "set in different branch": `const params = new URLSearchParams();
    if (flag) { params.set("visibility", "all"); } else { fetch("/v1/sessions?" + params); }`,
  "conditional parameter name": `const params = new URLSearchParams();
    params.set(flag ? "limit" : "visibility", "all"); fetch("/v1/sessions?" + params);`,
  "short-circuit set": `const params = new URLSearchParams();
    visibility && params.set("visibility", visibility); fetch("/v1/sessions?" + params);`,
  "late set": `const params = new URLSearchParams();
    fetch("/v1/sessions?" + params); params.set("visibility", "all");`,
  "string snapshot before set": `const params = new URLSearchParams();
    const url = "/v1/sessions?" + params; params.set("visibility", "all"); fetch(url);`,
  "deleted visibility": `const params = new URLSearchParams({ visibility: "all" });
    params.delete("visibility"); fetch("/v1/sessions?" + params);`,
  "deleted visibility through alias": `const params = new URLSearchParams({ visibility: "all" });
    const alias = params; alias.delete("visibility"); fetch("/v1/sessions?" + params);`,
  "alias sees deleted visibility": `const params = new URLSearchParams({ visibility: "all" });
    const alias = params; params.delete("visibility"); fetch("/v1/sessions?" + alias);`,
  "conditionally deleted visibility": `const params = new URLSearchParams({ visibility: "all" });
    if (flag) params.delete("visibility"); fetch("/v1/sessions?" + params);`,
  "reassigned query": `let params = new URLSearchParams({ visibility: "all" });
    params = new URLSearchParams({ limit: "25" }); fetch("/v1/sessions?" + params);`,
  "helper without visibility": `function query() { const params = new URLSearchParams({ limit: "25" });
    return params.toString(); } fetch("/v1/sessions?" + query());`,
  "helper with incomplete branch": `function query() { if (flag) return "visibility=all"; }
    fetch("/v1/sessions?" + query());`,
  "visibility in another parameter's value": `fetch("/v1/sessions?search_query=visibility=all");`,
  "visibility in fragment": `fetch("/v1/sessions?limit=25#visibility=all");`,
  "visibility in fragment query": `fetch("/v1/sessions#fragment?visibility=all");`,
  "trailing slash": `fetch("/v1/sessions/");`,
  "fetch ignores params option": `fetch("/v1/sessions", { params: { visibility: "all" } });`,
  "string spoofing suppression": `const note = "// custom-lint: disable-next=session-list-visibility";
    fetch("/v1/sessions");`,
  "template spoofing suppression":
    "const note = `// custom-lint: disable-next=session-list-visibility`;\nfetch('/v1/sessions');",
  "suppression on another line": `// custom-lint: disable=session-list-visibility
    fetch("/v1/sessions");`,
})) {
  test(`rejects ${name}`, () => {
    const findings = lint(source);
    assert.equal(findings.length, 1, JSON.stringify(findings));
    assert.equal(findings[0].path, "example.ts");
    assert.match(findings[0].message, /visibility/);
  });
}

test("reports the request line", () => {
  assert.equal(lint("const unused = 1;\n\nfetch('/v1/sessions');")[0].line, 3);
});

test("checks every branch of a URL", () => {
  assert.equal(
    lint(`fetch(flag ? "/v1/sessions?visibility=all" : "/v1/sessions")`).length,
    1,
  );
});

test("accepts TSX and ignores endpoint text inside JSX", () => {
  assert.deepEqual(
    analyzeSource("example.tsx", `<div>fetch('/v1/sessions')</div>`),
    [],
  );
});

for (const [filename, missing] of [
  ["web/src/hooks/useConversations.ts", 6],
  ["web/src/canvas/canvasSessions.ts", 1],
  ["web/src/extensions/services/sessions.ts", 1],
]) {
  test(`detects removal of visibility in actual ${filename} builders`, () => {
    const source = readFileSync(
      new URL(`../../${filename}`, import.meta.url),
      "utf8",
    );
    assert.deepEqual(analyzeSource(filename, source), []);
    const mutated = source
      .replace(
        /^\s*visibility: (?:visibility \?\? )?"(?:all|mine|shared|archived)",\n/gm,
        "",
      )
      .replace(/    visibility,\n    pinned: "true",/, '    pinned: "true",')
      .replace(/^\s*query\.set\("visibility", "all"\);\n/gm, "");
    assert.equal(analyzeSource(filename, mutated).length, missing);
  });
}

test("parse errors fail instead of silently passing", () => {
  assert.throws(
    () => lint("const params = ; fetch('/v1/sessions');"),
    /example.ts:1:/,
  );
});

test("CLI reports setup errors with nonzero exit", () => {
  const result = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./session_list_visibility.mjs", import.meta.url)),
      "/nonexistent/session-list-visibility-test.ts",
    ],
    { encoding: "utf8" },
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ENOENT/);
});

test("CLI emits JSON findings with exit zero", (context) => {
  const directory = mkdtempSync(path.join(tmpdir(), "session-visibility-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, "example.ts");
  writeFileSync(filename, 'fetch("/v1/sessions");');
  const result = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./session_list_visibility.mjs", import.meta.url)),
      filename,
    ],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0);
  assert.deepEqual(
    JSON.parse(result.stdout),
    analyzeSource(filename, readFileSync(filename, "utf8")),
  );
  assert.equal(result.stderr, "");
});
