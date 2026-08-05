// Local unit test for ready-for-review.js -- mocks the GitHub client and runs the
// real decision logic. No network.

const assert = require("assert");
const path = require("path");
const script = require(path.resolve(".github/workflows/ready-for-review.js"));

function pr({
  number,
  body = "",
  draft = false,
  labels = [],
  unlabeled = [],
}) {
  return {
    number,
    isDraft: draft,
    labels: { nodes: labels.map((name) => ({ name })) },
    body,
    // Each entry is a label name (removed by a human) or [name, actor].
    timelineItems: {
      nodes: unlabeled.map((u) =>
        Array.isArray(u)
          ? { label: { name: u[0] }, actor: { login: u[1] } }
          : { label: { name: u }, actor: { login: "maintainer1" } }
      ),
    },
  };
}

// `linked` maps PR number -> closing-issue count; `issues` maps number ->
// "issue" | "pr" | undefined (404).
async function run(
  nodes,
  { linked = {}, issues = {}, env = {}, linkError = false, failLabelOn = null } = {}
) {
  const labeled = [];
  const rows = [];
  let searchCalls = 0;
  const summary = {
    addHeading: () => summary,
    addRaw: () => summary,
    addTable: (t) => {
      rows.push(...t.slice(1));
      return summary;
    },
    write: async () => {},
  };
  const github = {
    graphql: async (query, vars) => {
      if (query.includes("pullRequest(number:")) {
        if (linkError) throw new Error("boom");
        return {
          repository: {
            pullRequest: { closingIssuesReferences: { totalCount: linked[vars.number] ?? 0 } },
          },
        };
      }
      const done = searchCalls++ > 0;
      return {
        rateLimit: { remaining: 4999, resetAt: "n/a" },
        search: { pageInfo: { hasNextPage: !done, endCursor: "c" }, nodes: done ? [] : nodes },
      };
    },
    rest: {
      issues: {
        addLabels: async ({ issue_number, labels: ls }) => {
          if (issue_number === failLabelOn) {
            const err = new Error("boom");
            err.status = 500;
            throw err;
          }
          labeled.push({ issue_number, labels: ls });
        },
        get: async ({ issue_number }) => {
          const kind = issues[issue_number];
          if (!kind) {
            const err = new Error("Not Found");
            err.status = 404;
            throw err;
          }
          return { data: kind === "pr" ? { pull_request: {} } : {} };
        },
      },
    },
  };
  const warnings = [];
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    await script({
      context: { repo: { owner: "o", repo: "r" } },
      github,
      core: { warning: (m) => warnings.push(m), summary },
    });
  } finally {
    for (const k of Object.keys(env)) delete process.env[k];
    Object.assign(process.env, saved);
  }
  return { labeled, rows, warnings };
}

const ENFORCE = { ENFORCE: "true" };
const verdictOf = (rows, n) => (rows.find((r) => r[0] === `#${n}`) || [])[1];

(async () => {
  // A fresh PR with a closing link clears the bar.
  {
    const { labeled } = await run([pr({ number: 10 })], { linked: { 10: 1 }, env: ENFORCE });
    assert.deepStrictEqual(labeled, [{ issue_number: 10, labels: [script.REVIEW_LABEL] }]);
  }

  // ...and so does a non-closing reference to a real issue, matching the nudge.
  {
    const { labeled } = await run([pr({ number: 11, body: "Part of #77" })], {
      issues: { 77: "issue" },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 1, "Part of #N clears the bar");
  }

  // A reference to another PR is not a tracking record.
  {
    const { labeled, rows } = await run([pr({ number: 12, body: "Refs #88" })], {
      issues: { 88: "pr" },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0);
    assert.strictEqual(verdictOf(rows, 12), "below bar");
  }

  // No reference at all: below the bar.
  {
    const { labeled, rows } = await run([pr({ number: 13 })], { env: ENFORCE });
    assert.strictEqual(labeled.length, 0);
    assert.strictEqual(verdictOf(rows, 13), "below bar");
  }

  // Draft: the author is saying it is not ready.
  {
    const { labeled, rows } = await run([pr({ number: 14, draft: true })], {
      linked: { 14: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0);
    assert.strictEqual(verdictOf(rows, 14), "skip");
  }

  // waiting-on-author wins: the two labels must never both be set.
  {
    const { labeled } = await run([pr({ number: 15, labels: ["waiting-on-author"] })], {
      linked: { 15: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, "never applied alongside waiting-on-author");
  }

  // Idempotent.
  {
    const { labeled } = await run([pr({ number: 16, labels: [script.REVIEW_LABEL] })], {
      linked: { 16: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, "no duplicate label");
  }

  // A maintainer who removed the label meant it; do not reapply every hour.
  {
    const { labeled, rows } = await run(
      [pr({ number: 17, unlabeled: [script.REVIEW_LABEL] })],
      { linked: { 17: 1 }, env: ENFORCE }
    );
    assert.strictEqual(labeled.length, 0, "respects a manual removal");
    assert.strictEqual(verdictOf(rows, 17), "skip");
  }
  // ...but an unrelated label removal is not a signal about this one.
  {
    const { labeled } = await run([pr({ number: 18, unlabeled: ["needs-demo"] })], {
      linked: { 18: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 1, "unrelated removals are ignored");
  }
  // The bot removes this label itself on every waiting-on-author transition, so
  // counting that would disqualify any PR that has been through a review round
  // trip. Observed on a real PR: unlabeled waiting-for-review by
  // github-actions[bot].
  {
    const { labeled } = await run(
      [pr({ number: 181, unlabeled: [[script.REVIEW_LABEL, "github-actions[bot]"]] })],
      { linked: { 181: 1 }, env: ENFORCE }
    );
    assert.strictEqual(labeled.length, 1, "a bot removal is not a human 'not ready'");
  }
  // A human removal still wins even when a bot also removed it earlier.
  {
    const { labeled } = await run(
      [
        pr({
          number: 182,
          unlabeled: [[script.REVIEW_LABEL, "github-actions[bot]"], script.REVIEW_LABEL],
        }),
      ],
      { linked: { 182: 1 }, env: ENFORCE }
    );
    assert.strictEqual(labeled.length, 0, "a human removal is still respected");
  }

  // One failed label write must not abandon the rest of the sweep.
  {
    const { labeled, warnings } = await run(
      [pr({ number: 191 }), pr({ number: 192 })],
      { linked: { 191: 1, 192: 1 }, env: ENFORCE, failLabelOn: 191 }
    );
    assert.deepStrictEqual(
      labeled.map((l) => l.issue_number),
      [192],
      "the sweep continues past a write failure"
    );
    assert.ok(warnings.some((w) => /Could not label #191/.test(w)));
  }

  // Dry run touches nothing but still reports.
  {
    const { labeled, rows } = await run([pr({ number: 19 })], { linked: { 19: 1 } });
    assert.strictEqual(labeled.length, 0, "dry run must not label");
    assert.strictEqual(verdictOf(rows, 19), "READY");
  }

  // An unverifiable link lookup must not label on a guess.
  {
    const { labeled, warnings } = await run([pr({ number: 20 })], {
      linkError: true,
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, "fails closed");
    assert.ok(warnings.some((w) => /Could not resolve links for #20/.test(w)));
  }

  // The scan never reaches back past the shared effective date.
  {
    const issueLink = require(path.resolve(".github/workflows/pr-issue-link.js"));
    assert.ok(issueLink.EFFECTIVE_FROM, "shares the issue-link effective date");
  }

  console.log("ready-for-review.test.js: all assertions passed");
})();
