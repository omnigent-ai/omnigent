// Local unit test for duplicate-prs.js -- mocks the GitHub client and runs the
// real decision logic. No network. The script paginates a GraphQL search and
// then closes/labels/comments the newer PRs for each over-subscribed issue.

const path = require("path");
const script = require(path.resolve(".github/workflows/duplicate-prs.js"));

// Build a PR node shaped like the GraphQL response. `issues` is the list of
// closing-issue references; `assoc` is the authorAssociation; `labels` is the
// label name list.
function pr({ number, createdAt, author = "ext", assoc = "CONTRIBUTOR", issues = [], labels = [] }) {
  return {
    number,
    createdAt,
    url: `https://example/pr/${number}`,
    author: { login: author },
    authorAssociation: assoc,
    labels: { nodes: labels.map((name) => ({ name })) },
    closingIssuesReferences: { nodes: issues.map((n) => ({ number: n })) },
  };
}

// Run the script against a set of PR nodes; returns the side effects.
async function run(nodes) {
  const closed = [];
  const labeled = [];
  const commented = [];
  let calls = 0;
  const github = {
    // Single page: first call returns the nodes, then stop.
    graphql: async () => {
      const done = calls++ > 0;
      return {
        rateLimit: { remaining: 4999, resetAt: "n/a" },
        search: {
          pageInfo: { hasNextPage: !done, endCursor: "c" },
          nodes: done ? [] : nodes,
        },
      };
    },
    rest: {
      pulls: {
        update: async ({ pull_number, state }) => closed.push({ pull_number, state }),
      },
      issues: {
        addLabels: async ({ issue_number, labels }) => labeled.push({ issue_number, labels }),
        createComment: async ({ issue_number, body }) => commented.push({ issue_number, body }),
      },
    },
  };
  const context = { repo: { owner: "omnigent-ai", repo: "omnigent" } };
  await script({ context, github });
  return {
    closed: closed.map((c) => c.pull_number).sort((a, b) => a - b),
    labeled: labeled.map((l) => l.issue_number).sort((a, b) => a - b),
    commented,
  };
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

(async () => {
  // 1. Two community PRs on the same issue: keep oldest (#1), close newer (#2).
  let r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [100] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [100] }),
  ]);
  assert("closes the newer duplicate, keeps the oldest",
    JSON.stringify(r.closed) === JSON.stringify([2]) &&
    JSON.stringify(r.labeled) === JSON.stringify([2]) &&
    r.commented.length === 1 && r.commented[0].body.includes("#1"),
    JSON.stringify(r));

  // 2. Three PRs on one issue: keep oldest, close the other two.
  r = await run([
    pr({ number: 5, createdAt: "2026-06-03T00:00:00Z", issues: [7] }),
    pr({ number: 3, createdAt: "2026-06-01T00:00:00Z", issues: [7] }),
    pr({ number: 4, createdAt: "2026-06-02T00:00:00Z", issues: [7] }),
  ]);
  assert("keeps oldest of three, closes the other two",
    JSON.stringify(r.closed) === JSON.stringify([4, 5]), JSON.stringify(r));

  // 3. Single PR per issue: nothing closed.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [1] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [2] }),
  ]);
  assert("distinct issues -> no closures", r.closed.length === 0, JSON.stringify(r));

  // 4a. Maintainer PR (older) is the keeper -> newer community duplicate closes.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9], assoc: "MEMBER" }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9] }),
  ]);
  assert("maintainer keeper -> newer community duplicate is closed",
    JSON.stringify(r.closed) === JSON.stringify([2]), JSON.stringify(r));

  // 4b. Community PR (older) keeper, maintainer PR (newer) duplicate -> the
  //     maintainer PR is flagged (heads-up comment + label) but never closed.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9], assoc: "MEMBER" }),
  ]);
  assert("maintainer duplicate is flagged, not closed",
    r.closed.length === 0 &&
    JSON.stringify(r.labeled) === JSON.stringify([2]) &&
    r.commented.length === 1 &&
    r.commented[0].issue_number === 2 &&
    r.commented[0].body.includes("won't be auto-closed"),
    JSON.stringify(r));

  // 4c. Two maintainer PRs on one issue -> neither is closed; the newer one is
  //     flagged with the heads-up comment.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9], assoc: "OWNER" }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9], assoc: "COLLABORATOR" }),
  ]);
  assert("two maintainer PRs -> none closed, newer flagged",
    r.closed.length === 0 &&
    JSON.stringify(r.labeled) === JSON.stringify([2]) &&
    r.commented.length === 1 && r.commented[0].issue_number === 2,
    JSON.stringify(r));

  // 4d. Mixed group: maintainer keeper + two community duplicates -> both close.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9], assoc: "MEMBER" }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9] }),
    pr({ number: 3, createdAt: "2026-06-03T00:00:00Z", issues: [9] }),
  ]);
  assert("maintainer keeper + 2 community dupes -> both community closed",
    JSON.stringify(r.closed) === JSON.stringify([2, 3]), JSON.stringify(r));

  // 5. Already-labeled duplicate is skipped (filtered before grouping).
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9], labels: ["duplicate"] }),
  ]);
  assert("already-labeled duplicate is skipped", r.closed.length === 0, JSON.stringify(r));

  // 6. PR referencing multiple issues is ambiguous -> skipped.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [9, 10] }),
  ]);
  assert("multi-issue PR is skipped, no duplicate group forms", r.closed.length === 0, JSON.stringify(r));

  // 7. PR with no issue reference is ignored.
  r = await run([
    pr({ number: 1, createdAt: "2026-06-01T00:00:00Z", issues: [9] }),
    pr({ number: 2, createdAt: "2026-06-02T00:00:00Z", issues: [] }),
  ]);
  assert("PR with no issue reference is ignored", r.closed.length === 0, JSON.stringify(r));
})();
