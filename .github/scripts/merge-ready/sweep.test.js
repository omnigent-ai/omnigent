const assert = require("node:assert/strict");
const test = require("node:test");
const sweep = require("./sweep.js");
const { expire } = require("./expiry.js");

const NOW = Date.parse("2026-10-29T12:00:00Z");
const EXPIRED = "All required checks green; expires 2026-10-29T11:59:59Z";
const FRESH = "All required checks green; expires 2026-10-29T12:00:01Z";
const context = { repo: { owner: "example", repo: "project" } };
const core = { info: () => {}, warning: () => {} };

function pullRequest(number, { description = EXPIRED, state = "SUCCESS" } = {}) {
  const sha = number.toString(16).padStart(40, "0");
  return {
    number,
    state: "OPEN",
    headRefOid: sha,
    commits: {
      nodes: [{
        commit: {
          oid: sha,
          status: { contexts: [{ context: "Merge Ready", state, description }] },
        },
      }],
    },
  };
}

function fixture(pages) {
  const queries = [];
  const dispatches = [];
  const github = {
    graphql: async (query, variables) => {
      const index = queries.length;
      queries.push({ query, variables });
      return {
        repository: {
          defaultBranchRef: { name: "trunk" },
          pullRequests: {
            nodes: pages[index],
            pageInfo: {
              hasNextPage: index < pages.length - 1,
              endCursor: `page-${index + 1}`,
            },
          },
        },
      };
    },
    rest: {
      actions: {
        createWorkflowDispatch: async (request) => dispatches.push(request),
      },
    },
  };
  return { github, queries, dispatches };
}

test("only expired or legacy successful Merge Ready statuses need reevaluation", async () => {
  const missingStatus = pullRequest(7);
  missingStatus.commits.nodes[0].commit.status = null;
  const otherStatus = pullRequest(8);
  otherStatus.commits.nodes[0].commit.status.contexts[0].context = "CI";
  const missingCommit = pullRequest(9);
  missingCommit.commits.nodes = [];
  const { github, dispatches } = fixture([[
    pullRequest(1),
    pullRequest(2, { description: "All required checks green; expires 2026-10-29T12:00:00Z" }),
    pullRequest(3, { description: FRESH }),
    pullRequest(4, { description: "All required checks green" }),
    pullRequest(5, { state: "FAILURE" }),
    pullRequest(6, { state: "PENDING" }),
    missingStatus,
    otherStatus,
    missingCommit,
  ]]);
  const result = await sweep({ github, context, core, now: NOW, dryRun: false });
  assert.equal(result.scanned, 9);
  assert.deepEqual(result.candidates.map((candidate) => candidate.number), [1, 2, 4]);
  assert.equal(result.dispatched, 3);
  assert.equal(result.deferred, 0);
  assert.deepEqual(dispatches, [1, 2, 4].map((number) => ({
    owner: "example",
    repo: "project",
    workflow_id: "merge-ready.yml",
    ref: "trunk",
    inputs: {
      pr: String(number), sha: pullRequest(number).headRefOid,
      expire_only: String(number !== 4),
    },
  })));
});

test("invalid expiry metadata is backfilled instead of preserving green forever", async () => {
  const descriptions = [
    null,
    "All required checks green; expires invalid",
    "All required checks green; expires 2026-02-30T12:00:00Z",
    "All required checks green; expires 2026-10-29T25:00:00Z",
  ];
  const { github } = fixture([descriptions.map((description, index) =>
    pullRequest(index + 1, { description }),
  )]);
  const result = await sweep({ github, context, core, now: NOW });
  assert.equal(result.candidates.length, descriptions.length);
  assert.ok(result.candidates.every((candidate) =>
    candidate.reason === "green status has no valid CI expiry",
  ));
});

test("draft, fork and unlabeled legacy PRs are included; stale head metadata is ignored", async () => {
  const draft = { ...pullRequest(1), isDraft: true };
  const fork = { ...pullRequest(2), isCrossRepository: true };
  const legacy = { ...pullRequest(3, { description: "All required checks green" }), labels: { nodes: [] } };
  const changedHead = pullRequest(4);
  changedHead.headRefOid = "f".repeat(40);
  const { github, queries } = fixture([[draft, fork, legacy, changedHead]]);
  const result = await sweep({ github, context, core, now: NOW });
  const connection = /pullRequests\(([^)]*)\)/.exec(queries[0].query);
  assert.ok(connection, "query reads the repository's pull request connection");
  assert.match(connection[1], /\bstates:\s*OPEN\b/);
  assert.deepEqual(
    [...connection[1].matchAll(/\b(\w+)\s*:/g)].map((match) => match[1]).sort(),
    ["after", "first", "states"],
    "open PR enumeration must not filter by labels, draft state or fork origin",
  );
  assert.deepEqual(result.candidates.map((candidate) => candidate.number), [1, 2, 3]);
});

test("all repository pages are scanned without the search API's 1000-result limit", async () => {
  const pages = Array.from({ length: 11 }, (_, page) =>
    Array.from({ length: 100 }, (_, index) => pullRequest(page * 100 + index + 1, { description: FRESH })),
  );
  pages.push([pullRequest(1101)]);
  const { github, queries, dispatches } = fixture(pages);
  const result = await sweep({ github, context, core, now: NOW, dryRun: false });
  assert.equal(result.scanned, 1101);
  assert.deepEqual(queries.map(({ variables }) => variables), pages.map((_, index) => ({
    owner: "example", repo: "project", cursor: index === 0 ? null : `page-${index}`,
  })));
  assert.deepEqual(dispatches.map((request) => request.inputs.pr), ["1101"]);
});

test("dry run is the default and makes no dispatches", async () => {
  const { github, dispatches } = fixture([[pullRequest(1)]]);
  const result = await sweep({ github, context, core, now: NOW });
  assert.equal(result.candidates.length, 1);
  assert.equal(result.dispatched, 0);
  assert.deepEqual(dispatches, []);
});

test("the dispatch budget bounds rollout and prioritizes expiry over legacy backfill on earlier pages", async () => {
  const legacy = Array.from({ length: 55 }, (_, index) =>
    pullRequest(index + 1, { description: "All required checks green" }),
  );
  const { github, dispatches } = fixture([legacy, [pullRequest(100)]]);
  const messages = [];
  const result = await sweep({
    github, context, now: NOW, dryRun: false,
    core: { ...core, info: (message) => messages.push(message) },
  });
  assert.equal(result.scanned, 56);
  assert.equal(result.candidates.length, 56);
  assert.equal(result.dispatched, 50);
  assert.equal(result.deferred, 6);
  assert.deepEqual(dispatches.map((request) => request.inputs.pr), [
    "100", ...Array.from({ length: 49 }, (_, index) => String(index + 1)),
  ]);
  assert.match(messages[0], /6 deferred beyond the 50-attempt limit/);
});

test("dry run reports every candidate even when the dispatch budget would defer some", async () => {
  const { github, dispatches } = fixture([[pullRequest(1), pullRequest(2)]]);
  const messages = [];
  const result = await sweep({
    github, context, now: NOW, maxDispatches: 1,
    core: { ...core, info: (message) => messages.push(message) },
  });
  assert.equal(result.candidates.length, 2);
  assert.equal(result.dispatched, 0);
  assert.equal(result.deferred, 1);
  assert.deepEqual(dispatches, []);
  assert.ok(messages.some((message) => message.startsWith("#1 ")));
  assert.ok(messages.some((message) => message.startsWith("#2 ")));
});

test("dispatch failures consume the attempt budget and leave deferred work visible", async () => {
  const { github } = fixture([[pullRequest(1), pullRequest(2), pullRequest(3)]]);
  const attempts = [];
  const messages = [];
  github.rest.actions.createWorkflowDispatch = async (request) => {
    attempts.push(request.inputs.pr);
    throw new Error("API unavailable");
  };
  await assert.rejects(
    sweep({
      github, context, now: NOW, dryRun: false, maxDispatches: 2,
      core: { ...core, info: (message) => messages.push(message) },
    }),
    /Failed to dispatch Merge Ready for PRs: 1, 2/,
  );
  assert.deepEqual(attempts, ["1", "2"]);
  assert.match(messages[0], /1 deferred beyond the 2-attempt limit/);
});

test("invalid dispatch budgets fail before reading or writing GitHub", async () => {
  const { github, queries, dispatches } = fixture([[pullRequest(1)]]);
  for (const maxDispatches of [0, -1, 1.5, Infinity]) {
    await assert.rejects(
      sweep({ github, context, core, now: NOW, maxDispatches }),
      /maxDispatches must be a positive integer/,
    );
  }
  assert.deepEqual(queries, []);
  assert.deepEqual(dispatches, []);
});

test("a failed dispatch does not prevent other expired PRs being refreshed", async () => {
  const { github } = fixture([[pullRequest(1), pullRequest(2)]]);
  const attempts = [];
  github.rest.actions.createWorkflowDispatch = async (request) => {
    attempts.push(request.inputs.pr);
    if (request.inputs.pr === "1") throw new Error("API unavailable");
  };
  await assert.rejects(
    sweep({ github, context, core, now: NOW, dryRun: false }),
    /Failed to dispatch Merge Ready for PRs: 1/,
  );
  assert.deepEqual(attempts, ["1", "2"]);
});

test("failed pagination produces no partial sweep writes", async () => {
  const { github, dispatches } = fixture([[pullRequest(1)], [pullRequest(2)]]);
  const graphql = github.graphql;
  github.graphql = async (query, variables) => {
    if (variables.cursor) throw new Error("API unavailable");
    return graphql(query, variables);
  };
  await assert.rejects(
    sweep({ github, context, core, now: NOW, dryRun: false }),
    /API unavailable/,
  );
  assert.deepEqual(dispatches, []);
});

test("invalid pagination fails instead of looping forever", async () => {
  const { github } = fixture([[pullRequest(1)], [pullRequest(2)]]);
  const graphql = github.graphql;
  github.graphql = async (query, variables) => {
    const result = await graphql(query, variables);
    result.repository.pullRequests.pageInfo.endCursor = null;
    return result;
  };
  await assert.rejects(
    sweep({ github, context, core, now: NOW }),
    /pagination did not advance/,
  );
});

function expiryApi(github, currentPull) {
  const queries = [];
  const writes = [];
  const graphql = github.graphql;
  github.graphql = async (query, variables) => {
    if (variables.pr === undefined) return graphql(query, variables);
    queries.push({ query, variables });
    return { repository: { pullRequest: currentPull } };
  };
  github.rest.repos = {
    createCommitStatus: async (request) => writes.push(request),
  };
  return { queries, writes };
}

test("sweep dispatch expires the current green status without enumerating CI", async () => {
  const pull = pullRequest(123);
  const { github, dispatches } = fixture([[pull]]);
  const { queries, writes } = expiryApi(github, pull);
  await sweep({ github, context, core, now: NOW, dryRun: false });
  const { inputs } = dispatches[0];
  assert.equal(inputs.expire_only, "true");
  assert.equal(await expire({
    github, context, core, pr: Number(inputs.pr), sha: inputs.sha, now: NOW,
  }), true);
  assert.equal(queries.length, 1);
  assert.deepEqual(queries[0].variables, { owner: "example", repo: "project", pr: 123 });
  assert.match(queries[0].query, /pullRequest\(number: \$pr\)/);
  assert.deepEqual(writes, [{
    owner: "example", repo: "project", sha: pull.headRefOid,
    context: "Merge Ready", state: "failure",
    description: "CI freshness not satisfied (24h); update branch and run fresh CI",
  }]);
});

test("a full evaluation refreshing green while expiry waits is preserved", async () => {
  const stale = pullRequest(1);
  const { github, dispatches } = fixture([[stale]]);
  const { writes } = expiryApi(github, pullRequest(1, { description: FRESH }));
  await sweep({ github, context, core, now: NOW, dryRun: false });
  assert.equal(dispatches.length, 1);
  const { inputs } = dispatches[0];
  assert.equal(await expire({
    github, context, core, pr: Number(inputs.pr), sha: inputs.sha, now: NOW,
  }), false);
  assert.deepEqual(writes, []);
});

test("expiry is inclusive and repeated jobs do not write the same failure again", async () => {
  const pull = pullRequest(1, { description: "All required checks green; expires 2026-10-29T12:00:00Z" });
  const { github } = fixture([]);
  const { writes } = expiryApi(github, pull);
  const args = { github, context, core, pr: 1, sha: pull.headRefOid, now: NOW };
  assert.equal(await expire(args), true);
  pull.commits.nodes[0].commit.status.contexts[0].state = "FAILURE";
  assert.equal(await expire(args), false);
  assert.equal(writes.length, 1);
});

test("obsolete, non-green, missing and unverifiable statuses are not invalidated", async () => {
  const closed = { ...pullRequest(1), state: "CLOSED" };
  const merged = { ...pullRequest(1), state: "MERGED" };
  const changedHead = { ...pullRequest(1), headRefOid: "f".repeat(40) };
  const missingStatus = pullRequest(1);
  missingStatus.commits.nodes[0].commit.status = null;
  const oldCommit = pullRequest(1);
  oldCommit.commits.nodes[0].commit.oid = "f".repeat(40);
  const otherStatus = pullRequest(1);
  otherStatus.commits.nodes[0].commit.status.contexts[0].context = "CI";
  for (const pull of [
    null, closed, merged, changedHead, oldCommit, missingStatus, otherStatus,
    pullRequest(1, { state: "FAILURE" }),
    pullRequest(1, { state: "PENDING" }),
    pullRequest(1, { description: FRESH }),
    pullRequest(1, { description: "All required checks green" }),
    pullRequest(1, { description: "All required checks green; expires invalid" }),
    pullRequest(1, { description: "All required checks green; expires 2026-02-30T12:00:00Z" }),
  ]) {
    const { github } = fixture([]);
    const { writes } = expiryApi(github, pull);
    assert.equal(await expire({
      github, context, core, pr: 1, sha: pullRequest(1).headRefOid, now: NOW,
    }), false);
    assert.deepEqual(writes, []);
  }
});

test("expiry API failures propagate without claiming a successful invalidation", async () => {
  for (const endpoint of ["read", "write"]) {
    const pull = pullRequest(1);
    const { github } = fixture([]);
    const { writes } = expiryApi(github, pull);
    const fail = async () => { throw new Error("API unavailable"); };
    if (endpoint === "read") github.graphql = fail;
    else github.rest.repos.createCommitStatus = fail;
    await assert.rejects(expire({
      github, context, core, pr: 1, sha: pull.headRefOid, now: NOW,
    }), /API unavailable/);
    assert.deepEqual(writes, []);
  }
});
