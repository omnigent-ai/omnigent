const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const script = require("./close-superseded-pr.js");

const REPO = "omnigent-ai/omnigent";
const MERGED = "2026-10-10T12:00:00Z";
function replacement(changes = {}) {
  return {
    number: 9296,
    merged: true,
    draft: false,
    merged_at: MERGED,
    body: "Closes #7947\n\nSupersedes #3993\nSupersedes #8135",
    user: { login: "omni-resolve-agent[bot]", type: "Bot" },
    base: { ref: "main", repo: { full_name: REPO } },
    head: { sha: "c".repeat(40) },
    ...changes,
  };
}
function source(pr = replacement()) {
  return {
    __typename: "PullRequest",
    number: pr.number,
    state: pr.merged ? "MERGED" : "OPEN",
    isDraft: pr.draft,
    body: pr.body,
    mergedAt: pr.merged_at,
    baseRefName: pr.base.ref,
    author: { login: pr.user.login, __typename: pr.user.type },
    repository: { nameWithOwner: pr.base.repo.full_name },
  };
}
function timeline(sources = [], more = false, cursor = null) {
  return {
    nodes: sources.map((s) => ({ source: s })),
    pageInfo: { hasNextPage: more, endCursor: cursor },
  };
}
function page(pulls, more = false, cursor = null) {
  return {
    repository: {
      pullRequests: {
        nodes: pulls,
        pageInfo: { hasNextPage: more, endCursor: cursor },
      },
    },
  };
}
function harness({
  pr = replacement(),
  event = true,
  oldStates = {},
  events = {},
  fail = null,
} = {}) {
  const pulls = new Map([
    [9296, pr],
    ...[3993, 8135].map((n) => [
      n,
      {
        number: n,
        state: oldStates[n] || "open",
        head: { sha: "a".repeat(40) },
      },
    ]),
  ]);
  const comments = new Map();
  const writes = [];
  const reads = [];
  const gh = {
    rest: { repos: {}, pulls: {}, issues: {} },
    graphql: async () => {
      throw new Error("Unexpected query");
    },
  };
  gh.rest.repos.get = async () => ({ data: { default_branch: "main" } });
  gh.rest.pulls.get = async ({ pull_number }) => {
    reads.push(pull_number);
    if (fail === pull_number) throw new Error("unavailable private diagnostic");
    assert(pulls.has(pull_number), `Missing PR ${pull_number}`);
    return { data: structuredClone(pulls.get(pull_number)) };
  };
  gh.rest.pulls.update = async ({ pull_number, state }) => {
    writes.push({ kind: "close", number: pull_number });
    assert.equal(state, "closed");
    pulls.get(pull_number).state = state;
  };
  gh.rest.issues.listComments = (params) => params;
  gh.rest.issues.listEventsForTimeline = (params) => params;
  gh.paginate = async (method, { issue_number }) => {
    if (method === gh.rest.issues.listComments)
      return comments.get(issue_number) || [];
    assert.equal(method, gh.rest.issues.listEventsForTimeline);
    return events[issue_number] || [];
  };
  gh.rest.issues.createComment = async ({ issue_number, body }) => {
    writes.push({ kind: "comment", number: issue_number });
    comments.set(issue_number, [
      ...(comments.get(issue_number) || []),
      {
        body,
        user: { type: "Bot", login: "github-actions[bot]" },
      },
    ]);
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: {
      ...(event ? { pull_request: { number: 9296, merged: true } } : {}),
    },
  };
  return { github: gh, context, pulls, comments, writes, reads };
}

test("parses all exact same-repository lines, preserving order and removing duplicates", () => {
  assert.deepEqual(
    script.supersededPRNumbers(
      "Supersedes #3993\nsupersedes #8135\nSupersedes #3993\nSupersedes other/repo#9\n" +
        "Supersedes https://github.com/other/repo/pull/9\nSupersedes #0\nSupersedes #999999999999999999999",
    ),
    [3993, 8135],
  );
});

test("merged replacement closes every target and skips one already closed", async () => {
  const h = harness({ oldStates: { 3993: "closed" } });
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(h.writes, [
    { kind: "comment", number: 8135 },
    { kind: "close", number: 8135 },
  ]);
  assert.deepEqual(
    report.results.map((r) => r.reason),
    ["already_closed", "merged_replacement"],
  );
  assert.equal(
    report.results[1].replacement_url,
    `https://github.com/${REPO}/pull/9296`,
  );
});

test("continues when an earlier target read fails, without leaking diagnostics", async () => {
  const h = harness({ fail: 3993 });
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(report.counts, { unknown: 1, closed: 1 });
  assert.equal(h.pulls.get(8135).state, "closed");
  assert(!JSON.stringify(report).includes("private diagnostic"));
});

for (const [name, changes] of Object.entries({
  open: { merged: false },
  abandoned: { merged: false, state: "closed" },
  draft: { draft: true },
  human: { user: { login: "omni-resolve-agent", type: "User" } },
  untrusted: { user: { login: "other-bot", type: "Bot" } },
  crossRepository: {
    base: { ref: "main", repo: { full_name: "elsewhere/omnigent" } },
  },
  releaseBranch: { base: { ref: "release", repo: { full_name: REPO } } },
  missingMergeTime: { merged_at: null },
})) {
  test(`does not close for ${name} replacement`, async () => {
    const h = harness({ pr: replacement(changes) });
    const report = await script({ ...h, dryRun: false });
    assert.deepEqual(h.writes, []);
    assert.equal(report.results[0].reason, "replacement_not_merged_or_trusted");
  });
}

test("ignores self references and ordinary mentions", async () => {
  const h = harness({
    pr: replacement({ body: "Supersedes #9296\nRelated to #3993" }),
  });
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(h.writes, []);
  assert.equal(report.results[0].reason, "self_reference");
});

test("uses fresh replacement data instead of stale merge payload", async () => {
  const h = harness({ pr: replacement({ merged: false }) });
  h.context.payload.pull_request = replacement();
  await script({ ...h, dryRun: false });
  assert.deepEqual(h.writes, []);
});

test("rechecks a replacement claim and target state after paginated reads", async () => {
  for (const change of ["remove_claim", "close_target"]) {
    const h = harness({ pr: replacement({ body: "Supersedes #8135" }) });
    const original = h.github.paginate;
    h.github.paginate = async (...args) => {
      const result = await original(...args);
      if (change === "remove_claim")
        h.pulls.get(9296).body = "No longer supersedes";
      else h.pulls.get(8135).state = "closed";
      return result;
    };
    await script({ ...h, dryRun: false });
    assert.deepEqual(h.writes, [], change);
  }
});

test("rerun after a close error reuses the explanation and retries only the closure", async () => {
  const h = harness({ pr: replacement({ body: "Supersedes #8135" }) });
  const original = h.github.rest.pulls.update;
  h.github.rest.pulls.update = async () => {
    throw new Error("lost response");
  };
  assert.equal((await script({ ...h, dryRun: false })).counts.unknown, 1);
  h.github.rest.pulls.update = original;
  assert.equal((await script({ ...h, dryRun: false })).counts.closed, 1);
  assert.equal((await script({ ...h, dryRun: false })).counts.skipped, 1);
  assert.deepEqual(h.writes, [
    { kind: "comment", number: 8135 },
    { kind: "close", number: 8135 },
  ]);
});

test("respects a reopen after replacement merged, including bot-mediated /reopen", async () => {
  const h = harness({
    events: {
      8135: [{ event: "reopened", created_at: "2026-10-11T01:00:00Z" }],
    },
  });
  const report = await script({ ...h, dryRun: false });
  assert.equal(report.results[1].reason, "reopened_after_replacement");
  assert.equal(h.pulls.get(8135).state, "open");
  assert.equal(h.pulls.get(3993).state, "closed");
});

test("default dry run and selected replacement never comment or close", async () => {
  const h = harness({ event: false });
  const report = await script({ ...h, replacementNumber: 9296 });
  assert.deepEqual(report.counts, { would_close: 2 });
  assert.deepEqual(h.writes, []);
});

test("scheduled catch-up pages open PRs and cross references, without an age cutoff", async () => {
  const h = harness({ event: false });
  const calls = [];
  h.github.graphql = async (query, params) => {
    assert.deepEqual(
      h.writes,
      [],
      "Complete discovery before changing the open-PR set",
    );
    calls.push({ query, params });
    if (params.number) {
      assert.equal(params.after, "refs-next");
      return {
        repository: {
          pullRequest: { timelineItems: timeline([source(), source()]) },
        },
      };
    }
    if (params.after)
      return page([
        { number: 8135, timelineItems: timeline([], true, "refs-next") },
      ]);
    return page(
      [
        {
          number: 3993,
          timelineItems: timeline([source(replacement({ merged: false }))]),
        },
      ],
      true,
      "prs-next",
    );
  };
  const report = await script({ ...h, dryRun: false });
  assert.equal(calls.length, 3);
  assert.equal(report.inspected, 2);
  assert.deepEqual(report.counts, { closed: 1 });
  assert.equal(h.pulls.get(3993).state, "open");
  assert.equal(h.pulls.get(8135).state, "closed");
});

test("incomplete timeline remains unknown, and later PRs still get processed", async () => {
  const h = harness({ event: false });
  h.github.graphql = async () =>
    page([
      { number: 3993, timelineItems: timeline([], true, null) },
      { number: 8135, timelineItems: timeline([source()]) },
    ]);
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(report.counts, { unknown: 1, closed: 1 });
});

test("repeated open-PR cursor stops with a visible discovery error", async () => {
  const h = harness({ event: false });
  h.github.graphql = async () => page([], true, "same");
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(report.counts, { unknown: 1 });
  assert.equal(report.results[0].reason, "discovery_failed");
});

test("sweep ignores untrusted, cross-repository and mismatched references", async () => {
  const h = harness({ event: false });
  h.github.graphql = async () =>
    page([
      {
        number: 8135,
        timelineItems: timeline([
          { __typename: "Issue" },
          source(
            replacement({
              user: { type: "User", login: "omni-resolve-agent" },
            }),
          ),
          source(
            replacement({
              base: { ref: "main", repo: { full_name: "elsewhere/omnigent" } },
            }),
          ),
          source(replacement({ body: "Supersedes #81350" })),
        ]),
      },
    ]);
  const report = await script({ ...h, dryRun: false });
  assert.deepEqual(report.results, []);
  assert.deepEqual(h.writes, []);
});

test("actual workflow script carries manual dry-run input through JSON report in a child process", () => {
  const workflow = fs.readFileSync(
    ".github/workflows/close-superseded-pr.yml",
    "utf8",
  );
  assert.match(
    workflow,
    /ref: \$\{\{ github.event.repository.default_branch \}\}/,
  );
  assert.match(workflow, /cancel-in-progress: false/);
  assert.match(workflow, /dry_run:[\s\S]*?default: true/);
  const inline = workflow
    .match(/          script: \|\n((?:            .*\n|\n)+)/)[1]
    .split("\n")
    .map((line) => line.slice(12))
    .join("\n");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "superseded-cleanup-"));
  try {
    for (const dryRun of ["true", "false"]) {
      const setup = `
        const gh = {
          rest: {repos: {get: async () => ({data: {default_branch: 'main'}})},
            pulls: {get: async ({pull_number}) => ({data: pull_number === 9296 ? ${JSON.stringify(replacement())} : {state:'open', head:{sha:'old'}}}),
              update: async () => { if (${JSON.stringify(dryRun)} === 'true') throw Error('Unexpected write'); }},
            issues: {listComments: 'comments', listEventsForTimeline: 'events',
              createComment: async () => { if (${JSON.stringify(dryRun)} === 'true') throw Error('Unexpected comment'); }}},
          paginate: async () => []
        };
        const github = gh;
        const context = {repo: {owner:'omnigent-ai',repo:'omnigent'},payload:{}};
        const core = {summary:{addHeading(){return this},addCodeBlock(){return this},async write(){}},setFailed(message){throw Error(message)}};
        (async () => { ${inline} })().catch(e => {console.error(e);process.exitCode=1});
      `;
      const result = spawnSync(process.execPath, ["-e", setup], {
        cwd: process.cwd(),
        encoding: "utf8",
        env: {
          ...process.env,
          DRY_RUN: dryRun,
          REPLACEMENT_PR: "9296",
          RUNNER_TEMP: dir,
        },
      });
      assert.equal(result.status, 0, result.stderr);
      const report = JSON.parse(
        fs.readFileSync(path.join(dir, "superseded-cleanup.json")),
      );
      assert.equal(report.dry_run, dryRun === "true");
      assert.deepEqual(report.counts, {
        [dryRun === "true" ? "would_close" : "closed"]: 2,
      });
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
