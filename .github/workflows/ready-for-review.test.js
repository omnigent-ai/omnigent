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
  state = "OPEN",
  author = "ext",
  assoc = "CONTRIBUTOR",
  bot = false,
}) {
  return {
    number,
    state,
    isDraft: draft,
    authorAssociation: assoc,
    author: { login: author, __typename: bot ? "Bot" : "User" },
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
  {
    linked = {},
    issues = {},
    env = {},
    linkError = false,
    failLabelOn = null,
    maintainers = [],
  } = {}
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
      if (query.includes("closingIssuesReferences")) {
        if (linkError) throw new Error("boom");
        return {
          repository: {
            pullRequest: { closingIssuesReferences: { totalCount: linked[vars.number] ?? 0 } },
          },
        };
      }
      // Single-PR fetch (the instant path).
      if (query.includes("createdAt")) {
        const found = nodes.find((n) => n.number === vars.number) ?? null;
        return {
          repository: {
            pullRequest: found ? { createdAt: "2026-08-06T00:00:00Z", ...found } : null,
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
      repos: {
        getContent: async () => ({
          data: { content: Buffer.from(maintainers.join("\n"), "utf8").toString("base64") },
        }),
      },
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
          // "issue" (open), "closed", "draft", or "pr".
          if (kind === "pr") return { data: { pull_request: {}, state: "open" } };
          if (kind === "closed") return { data: { state: "closed" } };
          if (kind === "draft") return { data: { state: "open", draft: true } };
          return { data: { state: "open" } };
        },
      },
    },
  };
  const warnings = [];
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    await script({
      context: {
        repo: { owner: "o", repo: "r" },
        payload: { repository: { default_branch: "main" } },
      },
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

  // A reference must resolve to an OPEN, non-draft issue. Shares the resolver
  // with the nudge, so the two cannot disagree about what counts.
  for (const [kind, why] of [
    ["pr", "a PR is not a tracking record"],
    ["closed", "a closed issue is not tracked work"],
    ["draft", "a draft issue is not agreed work yet"],
  ]) {
    const { labeled, rows } = await run([pr({ number: 12, body: "Refs #88" })], {
      issues: { 88: kind },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, why);
    assert.strictEqual(verdictOf(rows, 12), "below bar");
  }

  // A quoted example must not clear the bar either.
  {
    const { labeled } = await run(
      [pr({ number: 121, body: "> - `Part of #77` if this is one step towards it." })],
      { issues: { 77: "issue" }, env: ENFORCE }
    );
    assert.strictEqual(labeled.length, 0, "a blockquoted example does not clear the bar");
  }

  // No reference at all: below the bar.
  {
    const { labeled, rows } = await run([pr({ number: 13 })], { env: ENFORCE });
    assert.strictEqual(labeled.length, 0);
    assert.strictEqual(verdictOf(rows, 13), "below bar");
  }

  // The label routes incoming contributions, so the project's own work is skipped.
  for (const [who, opts] of [
    ["MEMBER", { assoc: "MEMBER" }],
    ["OWNER", { assoc: "OWNER" }],
    ["COLLABORATOR", { assoc: "COLLABORATOR" }],
    ["a bot", { bot: true, author: "omnigent-ci[bot]" }],
  ]) {
    const { labeled, rows } = await run([pr({ number: 30, ...opts })], {
      linked: { 30: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, `${who} PRs are not labelled`);
    assert.strictEqual(verdictOf(rows, 30), "skip");
  }
  // A maintainer with private org membership reads as CONTRIBUTOR, so the
  // MAINTAINER file is the second signal (same as the nudge).
  {
    const { labeled } = await run([pr({ number: 31, author: "listed-maintainer" })], {
      linked: { 31: 1 },
      env: ENFORCE,
      maintainers: ["listed-maintainer", "# a comment"],
    });
    assert.strictEqual(labeled.length, 0, "the MAINTAINER file also exempts");
  }
  // ...but a genuine outside contributor still gets the label.
  {
    const { labeled } = await run([pr({ number: 32, author: "outsider" })], {
      linked: { 32: 1 },
      env: ENFORCE,
      maintainers: ["listed-maintainer"],
    });
    assert.strictEqual(labeled.length, 1, "contributors are still labelled");
  }

  // `is:open` in the search lags, so a just-closed or merged PR still comes back.
  for (const state of ["CLOSED", "MERGED"]) {
    const { labeled, rows } = await run([pr({ number: 33, state })], {
      linked: { 33: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(labeled.length, 0, `${state} PRs are not labelled`);
    assert.strictEqual(verdictOf(rows, 33), "skip");
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

  // ---- the instant path: PR_NUMBER names one PR ----
  {
    const nodes = [pr({ number: 70 }), pr({ number: 71 })];
    const { labeled } = await run(nodes, {
      linked: { 70: 1, 71: 1 },
      env: { ...ENFORCE, PR_NUMBER: "70" },
    });
    assert.deepStrictEqual(
      labeled.map((l) => l.issue_number),
      [70],
      "only the named PR is labelled"
    );
  }
  // Exclusions still hold on the instant path.
  {
    const { labeled } = await run([pr({ number: 72, assoc: "MEMBER" })], {
      linked: { 72: 1 },
      env: { ...ENFORCE, PR_NUMBER: "72" },
    });
    assert.strictEqual(labeled.length, 0, "maintainer PRs stay skipped");
  }
  // The effective-date floor applies to events too.
  {
    const old = pr({ number: 73 });
    old.createdAt = "2026-07-01T00:00:00Z";
    const { labeled } = await run([old], {
      linked: { 73: 1 },
      env: { ...ENFORCE, PR_NUMBER: "73" },
    });
    assert.strictEqual(labeled.length, 0, "a pre-cutoff PR is skipped");
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
