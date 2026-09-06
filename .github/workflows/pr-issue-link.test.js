// Local unit test for pr-issue-link.js -- mocks the GitHub client and runs the
// real decision logic. No network. Covers the exemption predicates, the
// authoritative per-PR link lookup, the needs-issue label as dedupe and close
// clock, and that a dry run touches nothing.

const assert = require("assert");
const path = require("path");
const script = require(path.resolve(".github/workflows/pr-issue-link.js"));

// A PR node shaped like the GraphQL search response.
function pr({
  number,
  body = "",
  title = "feat: thing",
  author = "ext",
  assoc = "CONTRIBUTOR",
  bot = false,
  draft = false,
  additions = 100,
  deletions = 0,
  labels = [],
}) {
  return {
    number,
    title,
    isDraft: draft,
    additions,
    deletions,
    authorAssociation: assoc,
    author: { login: author, __typename: bot ? "Bot" : "User" },
    labels: { nodes: labels.map((name) => ({ name })) },
    body,
  };
}

// Days ago as an ISO stamp, for the label clock.
function daysAgo(n) {
  return new Date(Date.now() - n * 24 * 60 * 60 * 1000).toISOString();
}

// Run the script over PR nodes. `linked` maps PR number -> closing-issue count.
// `env` overrides process.env for the run. `labelNodes` are served to the
// second (label:needs-issue) search pass; `labelEvents` maps PR number -> when
// the label went on, which is the close clock.
async function run(
  nodes,
  {
    linked = {},
    env = {},
    linkError = false,
    maintainers = [],
    labelNodes = [],
    labelEvents = {},
    issues = {},
  } = {}
) {
  const commented = [];
  const labeled = [];
  const unlabeled = [];
  const updated = [];
  const createdLabels = [];
  const queries = [];
  const pages = {};
  const github = {
    repos: {},
    graphql: async (query, vars) => {
      if (vars.searchQuery) queries.push(vars.searchQuery);
      // ONE_PR_QUERY also contains "pullRequest(number:", so match on the field
      // that is unique to the link lookup.
      if (query.includes("closingIssuesReferences")) {
        if (linkError) throw new Error("boom");
        return {
          repository: {
            pullRequest: {
              closingIssuesReferences: { totalCount: linked[vars.number] ?? 0 },
            },
          },
        };
      }
      // Single-PR fetch (the instant path).
      if (query.includes("createdAt")) {
        const pr = nodes.find((n) => n.number === vars.number) ?? null;
        return {
          repository: {
            pullRequest: pr
              ? { state: "OPEN", createdAt: "2026-08-06T00:00:00Z", ...pr }
              : null,
          },
        };
      }
      // Two passes run per sweep: the 24-hour window, then label:needs-issue.
      // Each serves its nodes on page 1 and an empty page 2, to exercise paging.
      const pass = /label:/.test(vars.searchQuery ?? "") ? "label" : "window";
      pages[pass] = (pages[pass] ?? 0) + 1;
      const first = pages[pass] === 1;
      return {
        rateLimit: { remaining: 4999, resetAt: "n/a" },
        search: {
          pageInfo: { hasNextPage: first, endCursor: "c" },
          nodes: first ? (pass === "label" ? labelNodes : nodes) : [],
        },
      };
    },
    paginate: async (fn, { issue_number }) => {
      if (fn !== "listEvents") return [];
      const at = labelEvents[issue_number];
      return at
        ? [{ event: "labeled", label: { name: script.NEEDS_ISSUE_LABEL }, created_at: at }]
        : [];
    },
    rest: {
      repos: {
        getContent: async () => ({
          data: { content: Buffer.from(maintainers.join("\n"), "utf8").toString("base64") },
        }),
      },
      issues: {
        listComments: "listComments",
        listEvents: "listEvents",
        createComment: async ({ issue_number, body }) => commented.push({ issue_number, body }),
        addLabels: async ({ issue_number, labels: ls }) => labeled.push({ issue_number, labels: ls }),
        removeLabel: async ({ issue_number, name }) => unlabeled.push({ issue_number, name }),
        update: async ({ issue_number, state }) => updated.push({ issue_number, state }),
        createLabel: async ({ name }) => createdLabels.push(name),
        // `issues` maps number -> "issue" | "pr" | undefined (404).
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
  // Capture the step-summary table rows so the dry-run verdict list can be
  // asserted on (the rows are `[#N, verdict, reason]` after the header).
  const rows = [];
  const summary = {
    addHeading: () => summary,
    addRaw: () => summary,
    addTable: (table) => {
      rows.push(...table.slice(1));
      return summary;
    },
    write: async () => {},
  };
  const core = { warning: (m) => warnings.push(m), summary };
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    await script({
      context: { repo: { owner: "o", repo: "r" }, payload: { repository: { default_branch: "main" } } },
      github,
      core,
    });
  } finally {
    for (const k of Object.keys(env)) delete process.env[k];
    Object.assign(process.env, saved);
  }
  return { commented, labeled, unlabeled, updated, createdLabels, warnings, rows, queries };
}

const ENFORCE = { ENFORCE: "true" };

// ---- exemption predicates (pure) ----
const { exemptReason } = script;

assert.strictEqual(exemptReason(pr({ number: 1 })), null, "plain unlinked PR is not exempt");
assert.strictEqual(exemptReason(pr({ number: 2, bot: true })), "bot");
assert.strictEqual(exemptReason(pr({ number: 3, draft: true })), "draft");

// Maintainers are exempt via EITHER signal. Both are needed: a maintainer with
// private org membership reads as CONTRIBUTOR, and a maintainer with write
// access may not be listed in .github/MAINTAINER.
for (const assoc of ["MEMBER", "OWNER", "COLLABORATOR"]) {
  assert.strictEqual(
    exemptReason(pr({ number: 30, assoc })),
    "maintainer",
    `${assoc} is exempt by association`
  );
}
assert.strictEqual(
  exemptReason(pr({ number: 31, author: "Maintainer-Person", assoc: "CONTRIBUTOR" }), new Set(["maintainer-person"])),
  "maintainer",
  "MAINTAINER file catches a private-membership maintainer (case-insensitive)"
);
assert.strictEqual(
  exemptReason(pr({ number: 32, author: "outsider" }), new Set(["maintainer-person"])),
  null,
  "a non-maintainer is still enforced"
);
assert.strictEqual(
  exemptReason(pr({ number: 4, labels: ["skip-issue-check"] })),
  "skip-issue-check label"
);
assert.strictEqual(
  exemptReason(pr({ number: 5, additions: 4, deletions: 5 })),
  "trivial",
  "<= 9 changed lines is trivial"
);
assert.strictEqual(
  exemptReason(pr({ number: 6, additions: 6, deletions: 5 })),
  null,
  "10 changed lines is not trivial"
);
assert.strictEqual(exemptReason(pr({ number: 7, title: "Revert \"feat: x\"" })), "revert");
// There is no self-service opt-out: writing `no-issue` in the body does nothing.
assert.strictEqual(exemptReason(pr({ number: 8, body: "blah\nno-issue\nblah" })), null);

// Declared exempt types, matching the real template's checkbox labels.
for (const type of ["Refactor / chore", "Docs", "Test / CI"]) {
  assert.strictEqual(
    exemptReason(pr({ number: 9, body: `## Type of change\n\n- [x] ${type}\n` })),
    "declared chore/docs/test",
    `${type} checked is exempt`
  );
}
// The whole point of the gate: silence must NOT exempt.
assert.strictEqual(
  exemptReason(
    pr({
      number: 10,
      body: "## Type of change\n\n- [ ] Bug fix\n- [ ] Refactor / chore\n- [ ] Docs\n- [ ] Test / CI\n",
    })
  ),
  null,
  "unchecked boxes do not exempt"
);
assert.strictEqual(
  exemptReason(pr({ number: 11, body: "no template at all" })),
  null,
  "a deleted template does not exempt"
);
assert.strictEqual(
  exemptReason(pr({ number: 12, body: "## Type of change\n\n- [x] Bug fix\n- [ ] Docs\n" })),
  null,
  "a declared Bug fix is not exempt"
);
// Ticking an exempt box alongside a tracked one must not buy an opt-out.
for (const tracked of ["Bug fix", "Feature", "UI / frontend change"]) {
  assert.strictEqual(
    exemptReason(
      pr({ number: 13, body: `## Type of change\n\n- [x] ${tracked}\n- [x] Test / CI\n` })
    ),
    null,
    `${tracked} + Test / CI is not exempt`
  );
}

// ---- tracking-reference parsing (pure) ----
{
  const { trackingReferences: refs } = script;
  assert.deepStrictEqual(refs("Refs #3644"), [3644], "Refs #N");
  assert.deepStrictEqual(refs("Part of #123"), [123], "Part of #N");
  assert.deepStrictEqual(refs("blah\nRelated to #5\nblah"), [5], "Related to #N");
  assert.deepStrictEqual(refs("Towards #9"), [9], "Towards #N");
  assert.deepStrictEqual(
    refs("Part of https://github.com/omnigent-ai/omnigent/issues/321"),
    [321],
    "full issue URL"
  );
  assert.deepStrictEqual(refs("Refs omnigent-ai/omnigent#77"), [77], "cross-repo ref");
  assert.deepStrictEqual(refs("Part of #7 and refs #7"), [7], "dedupes");
  // A bare mention is a cross-reference, not a statement about this PR.
  assert.deepStrictEqual(refs("similar to #77 maybe"), [], "bare #N does not count");
  assert.deepStrictEqual(refs("this fixes the thing generally"), [], "prose does not count");
  assert.deepStrictEqual(refs(""), [], "empty body");
  assert.deepStrictEqual(refs(undefined), [], "missing body");
  // Quoted and fenced text is shown, not asserted.
  assert.deepStrictEqual(refs("> Part of #123"), [], "blockquote excluded");
  assert.deepStrictEqual(refs("  > - `Part of #123` example"), [], "indented blockquote excluded");
  assert.deepStrictEqual(refs("```\nPart of #123\n```"), [], "fenced block excluded");
  assert.deepStrictEqual(refs("~~~\nRefs #123\n~~~"), [], "tilde fence excluded");
  assert.deepStrictEqual(refs("> quoted #9\n\nPart of #7"), [7], "keeps the asserted one");
  // An unterminated fence swallows the rest, which is the safe direction.
  assert.deepStrictEqual(refs("```\nPart of #5"), [], "unterminated fence excluded");
}

// ---- end-to-end behaviour ----
(async () => {
  // Forward-only: the search must never reach past the effective date, so the
  // pre-existing backlog can't be flagged.
  {
    const { queries } = await run([pr({ number: 19 })]);
    const floor = new Date(script.EFFECTIVE_FROM).getTime();
    const asked = new Date(/created:>(\S+)/.exec(queries[0])[1]).getTime();
    assert.ok(asked >= floor, "scan cutoff never predates the effective date");
  }

  // SCAN_HOURS widens the window for a one-off backfill, but never past the
  // effective date: the pre-rule backlog stays unreachable however wide it is set.
  {
    const { queries } = await run([pr({ number: 190 })], { env: { SCAN_HOURS: "100000" } });
    const floor = new Date(script.EFFECTIVE_FROM).getTime();
    const asked = new Date(/created:>(\S+)/.exec(queries[0])[1]).getTime();
    assert.strictEqual(asked, floor, "a wide window clamps to the effective date");
  }

  // A malformed SCAN_HOURS falls back to the default window rather than widening.
  {
    const { queries, warnings } = await run([pr({ number: 191 })], {
      env: { SCAN_HOURS: "lots" },
    });
    const dayAgo = Date.now() - 25 * 60 * 60 * 1000;
    const asked = new Date(/created:>(\S+)/.exec(queries[0])[1]).getTime();
    assert.ok(asked > dayAgo, "malformed SCAN_HOURS keeps the 24-hour window");
    assert.ok(warnings.some((w) => /SCAN_HOURS=lots/.test(w)));
  }

  // Dry run (the default) must not comment or label.
  {
    const { commented, labeled } = await run([pr({ number: 20 })]);
    assert.strictEqual(commented.length, 0, "dry run must not comment");
    assert.strictEqual(labeled.length, 0, "dry run must not label");
  }

  // Enforcing: an unlinked, non-exempt PR gets exactly one comment and no label.
  {
    const { commented, labeled } = await run([pr({ number: 21, author: "alice" })], { env: ENFORCE });
    assert.strictEqual(commented.length, 1);
    assert.strictEqual(commented[0].issue_number, 21);
    assert.match(commented[0].body, /@alice/);
    assert.match(commented[0].body, /Closes #123/);
    assert.ok(commented[0].body.startsWith(script.MARKER), "comment carries the provenance marker");
    // House style: no em dashes in anything a contributor reads.
    assert.ok(!commented[0].body.includes("—"), "no em dashes in the nudge");
    // The exemption must not read as a free opt-out.
    assert.match(commented[0].body, /require an issue for every PR/);
    assert.match(commented[0].body, /even when it also touches docs or tests/);
    // The close is a promise the comment has to make before the clock can run.
    assert.match(commented[0].body, /will be closed automatically/);
    assert.match(commented[0].body, new RegExp(`${script.CLOSE_AFTER_DAYS} days`));
    assert.match(commented[0].body, /\/reopen/);
    assert.deepStrictEqual(
      labeled,
      [{ issue_number: 21, labels: [script.NEEDS_ISSUE_LABEL] }],
      "the nudge starts the clock by labeling"
    );
  }

  // A non-closing reference to a real ISSUE satisfies the rule: a PR that only
  // partly addresses an issue should not have to claim it closes it.
  for (const kw of ["Part of #77", "Related to #77", "Towards #77", "Refs #77", "See #77"]) {
    const { commented } = await run([pr({ number: 50, body: `Work here.\n\n${kw}` })], {
      env: ENFORCE,
      issues: { 77: "issue" },
    });
    assert.strictEqual(commented.length, 0, `${kw} must satisfy the rule`);
  }

  // ...but only when it resolves to an OPEN, non-draft issue.
  for (const [kind, why] of [
    ["pr", "a reference to a PR does not count"],
    ["closed", "a closed issue is not tracked work"],
    ["draft", "a draft issue is not agreed work yet"],
  ]) {
    const { commented } = await run([pr({ number: 51, body: "Refs #88" })], {
      env: ENFORCE,
      issues: { 88: kind },
    });
    assert.strictEqual(commented.length, 1, why);
  }

  // Quoted or fenced text is shown, not asserted. A PR that documents the bot's
  // own comment must not satisfy its own rule -- this fired on a real PR.
  {
    const quoted = "See the wording:\n\n> - `Part of #77` if this is one step towards it.\n";
    const { commented } = await run([pr({ number: 54, body: quoted })], {
      env: ENFORCE,
      issues: { 77: "issue" },
    });
    assert.strictEqual(commented.length, 1, "a blockquoted example does not count");
  }
  {
    const fenced = "Example:\n\n```\nPart of #77\n```\n";
    const { commented } = await run([pr({ number: 55, body: fenced })], {
      env: ENFORCE,
      issues: { 77: "issue" },
    });
    assert.strictEqual(commented.length, 1, "a fenced example does not count");
  }
  // A real reference alongside a quoted one still counts.
  {
    const both = "> quoting `Part of #99` here\n\nPart of #77\n";
    const { commented } = await run([pr({ number: 56, body: both })], {
      env: ENFORCE,
      issues: { 77: "issue", 99: "issue" },
    });
    assert.strictEqual(commented.length, 0, "an asserted reference still counts");
  }

  // A bare mention is a cross-reference, not a claim about this PR.
  {
    const { commented } = await run([pr({ number: 52, body: "similar to #77 maybe" })], {
      env: ENFORCE,
      issues: { 77: "issue" },
    });
    assert.strictEqual(commented.length, 1, "a bare #N does not count");
  }

  // An unresolvable number proves nothing; keep checking the rest.
  {
    const { commented } = await run([pr({ number: 53, body: "Refs #999\nPart of #77" })], {
      env: ENFORCE,
      issues: { 77: "issue" },
    });
    assert.strictEqual(commented.length, 0, "falls through to the next candidate");
  }

  // ---- the instant path: PR_NUMBER names one PR ----
  // Same verdict as the sweep would reach, so the two routes cannot disagree.
  {
    const nodes = [pr({ number: 60, author: "alice" }), pr({ number: 61 })];
    const { commented } = await run(nodes, { env: { ...ENFORCE, PR_NUMBER: "60" } });
    assert.deepStrictEqual(
      commented.map((c) => c.issue_number),
      [60],
      "only the named PR is touched"
    );
  }
  // An exempt PR named by an event is still exempt.
  {
    const { commented } = await run([pr({ number: 62, assoc: "MEMBER" })], {
      env: { ...ENFORCE, PR_NUMBER: "62" },
    });
    assert.strictEqual(commented.length, 0, "the instant path honours exemptions");
  }
  // The effective-date floor still applies: an event is not a licence to reach
  // into the backlog.
  {
    const old = pr({ number: 63 });
    old.createdAt = "2026-07-01T00:00:00Z";
    const { commented } = await run([old], { env: { ...ENFORCE, PR_NUMBER: "63" } });
    assert.strictEqual(commented.length, 0, "a pre-cutoff PR is skipped");
  }
  // A PR that closed between the event and the run is left alone.
  {
    const closed = pr({ number: 64 });
    closed.state = "CLOSED";
    const { commented } = await run([closed], { env: { ...ENFORCE, PR_NUMBER: "64" } });
    assert.strictEqual(commented.length, 0, "a closed PR is skipped");
  }
  // An unknown number is a no-op rather than a crash.
  {
    const { commented } = await run([pr({ number: 65 })], {
      env: { ...ENFORCE, PR_NUMBER: "999" },
    });
    assert.strictEqual(commented.length, 0, "an unresolvable PR number is a no-op");
  }

  // A linked PR is left alone even when enforcing.
  {
    const { commented, labeled } = await run([pr({ number: 22 })], {
      linked: { 22: 1 },
      env: ENFORCE,
    });
    assert.strictEqual(commented.length, 0, "linked PR must not be flagged");
    assert.strictEqual(labeled.length, 0);
  }

  // An already-labeled PR is never nudged twice: the label is the dedupe.
  {
    const flagged = pr({ number: 23, labels: [script.NEEDS_ISSUE_LABEL] });
    const { commented } = await run([flagged], {
      env: ENFORCE,
      labelEvents: { 23: daysAgo(1) },
    });
    assert.strictEqual(commented.length, 0, "the label dedupes repeat runs");
  }

  // Inside the window the PR is left alone, with the countdown reported.
  {
    const flagged = pr({ number: 230, labels: [script.NEEDS_ISSUE_LABEL] });
    const { commented, updated, rows } = await run([flagged], {
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
      labelEvents: { 230: daysAgo(script.CLOSE_AFTER_DAYS - 1) },
    });
    assert.strictEqual(commented.length, 0, "not yet due");
    assert.strictEqual(updated.length, 0, "not yet due");
    assert.match(rows.find((r) => r[0] === "#230")[1], /waiting/);
  }

  // Past the window, CLOSE_ENFORCE off is a dry run: it reports, changes nothing.
  {
    const flagged = pr({ number: 232, labels: [script.NEEDS_ISSUE_LABEL] });
    const { commented, updated, rows } = await run([flagged], {
      env: ENFORCE,
      labelEvents: { 232: daysAgo(script.CLOSE_AFTER_DAYS + 3) },
    });
    assert.strictEqual(updated.length, 0, "CLOSE_ENFORCE off must not close");
    assert.strictEqual(commented.length, 0, "CLOSE_ENFORCE off must not comment");
    assert.strictEqual(rows.find((r) => r[0] === "#232")[1], "WOULD CLOSE");
  }

  // Past the window with CLOSE_ENFORCE on: comment, then close.
  {
    const flagged = pr({ number: 233, labels: [script.NEEDS_ISSUE_LABEL] });
    const { commented, updated } = await run([flagged], {
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
      labelEvents: { 233: daysAgo(script.CLOSE_AFTER_DAYS) },
    });
    assert.deepStrictEqual(updated, [{ issue_number: 233, state: "closed" }]);
    assert.strictEqual(commented.length, 1);
    assert.match(commented[0].body, /\/reopen/, "the close is reversible, and says so");
    assert.ok(!commented[0].body.includes("\u2014"), "no em dashes in the close notice");
  }

  // CLOSE_ENFORCE alone must never close: it only unlocks the enforcing path.
  {
    const flagged = pr({ number: 234, labels: [script.NEEDS_ISSUE_LABEL] });
    const { updated, commented } = await run([flagged], {
      env: { CLOSE_ENFORCE: "true" },
      labelEvents: { 234: daysAgo(30) },
    });
    assert.strictEqual(updated.length, 0, "CLOSE_ENFORCE without ENFORCE is still a dry run");
    assert.strictEqual(commented.length, 0);
  }

  // A label with no timestamp in the timeline must fail closed, never close.
  {
    const flagged = pr({ number: 235, labels: [script.NEEDS_ISSUE_LABEL] });
    const { updated, warnings } = await run([flagged], {
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
    });
    assert.strictEqual(updated.length, 0, "no label timestamp is not grounds to close");
    assert.ok(warnings.some((w) => /#235 has needs-issue but no labeled event/.test(w)));
  }

  // The label is derived state: linking the issue clears it and the countdown.
  {
    const flagged = pr({ number: 236, labels: [script.NEEDS_ISSUE_LABEL] });
    const { unlabeled, updated } = await run([flagged], {
      linked: { 236: 1 },
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
      labelEvents: { 236: daysAgo(30) },
    });
    assert.deepStrictEqual(unlabeled, [
      { issue_number: 236, name: script.NEEDS_ISSUE_LABEL },
    ]);
    assert.strictEqual(updated.length, 0, "a linked PR is never closed");
  }

  // Becoming exempt also clears it, so a draft's clock does not keep running.
  {
    const flagged = pr({ number: 237, draft: true, labels: [script.NEEDS_ISSUE_LABEL] });
    const { unlabeled, updated } = await run([flagged], {
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
      labelEvents: { 237: daysAgo(30) },
    });
    assert.deepStrictEqual(unlabeled, [
      { issue_number: 237, name: script.NEEDS_ISSUE_LABEL },
    ]);
    assert.strictEqual(updated.length, 0, "an exempt PR is never closed");
  }

  // The label pass is what reaches a PR older than the 24-hour window.
  {
    const stale = pr({ number: 238, labels: [script.NEEDS_ISSUE_LABEL] });
    const { updated, queries } = await run([], {
      labelNodes: [stale],
      env: { ...ENFORCE, CLOSE_ENFORCE: "true" },
      labelEvents: { 238: daysAgo(20) },
    });
    assert.ok(
      queries.some((q) => q.includes(`label:${script.NEEDS_ISSUE_LABEL}`)),
      "a second search pass looks for already-flagged PRs"
    );
    assert.deepStrictEqual(updated, [{ issue_number: 238, state: "closed" }]);
  }

  // LIMIT caps closures the same way it caps nudges.
  {
    const overdue = [239, 240, 241].map((n) => pr({ number: n, labels: [script.NEEDS_ISSUE_LABEL] }));
    const { updated } = await run(overdue, {
      env: { ...ENFORCE, CLOSE_ENFORCE: "true", LIMIT: "2" },
      labelEvents: { 239: daysAgo(10), 240: daysAgo(10), 241: daysAgo(10) },
    });
    assert.strictEqual(updated.length, 2, "LIMIT bounds closures per run");
  }

  // A failed link lookup must fail closed (skip), never flag.
  {
    const { commented, warnings } = await run([pr({ number: 24 })], {
      env: ENFORCE,
      linkError: true,
    });
    assert.strictEqual(commented.length, 0, "unverifiable PR must not be flagged");
    assert.ok(warnings.some((w) => /Could not resolve links for #24/.test(w)));
  }

  // LIMIT caps how many PRs a single run touches.
  {
    const nodes = [25, 26, 27].map((number) => pr({ number }));
    const { commented, rows } = await run(nodes, { env: { ...ENFORCE, LIMIT: "2" } });
    assert.strictEqual(commented.length, 2, "LIMIT caps flags per run");
    assert.ok(
      rows.some((r) => r[1] === "deferred"),
      "the PR past the cap is reported as deferred"
    );
  }

  // LIMIT must NOT truncate a dry run: reviewing the full list before enabling
  // is the entire point of the dry run.
  {
    const nodes = [40, 41, 42].map((number) => pr({ number }));
    const { commented, rows } = await run(nodes, { env: { LIMIT: "1" } });
    assert.strictEqual(commented.length, 0, "dry run still touches nothing");
    assert.strictEqual(
      rows.filter((r) => r[1] === "FLAG").length,
      3,
      "dry run enumerates every flaggable PR regardless of LIMIT"
    );
  }

  // An explicit LIMIT=0 means flag nothing (not unlimited).
  {
    const { commented } = await run([pr({ number: 43 })], { env: { ...ENFORCE, LIMIT: "0" } });
    assert.strictEqual(commented.length, 0, "LIMIT=0 flags nothing");
  }

  // A malformed LIMIT must fail toward flagging nothing, not everything.
  {
    const { commented, warnings } = await run([pr({ number: 44 })], {
      env: { ...ENFORCE, LIMIT: "abc" },
    });
    assert.strictEqual(commented.length, 0, "malformed LIMIT flags nothing");
    assert.ok(warnings.some((w) => /not a number/.test(w)), "and says so");
  }

  // Maintainer PRs are never commented on, by either signal.
  {
    const { commented } = await run(
      [
        pr({ number: 28, assoc: "MEMBER" }),
        pr({ number: 29, author: "listed-maintainer" }),
        pr({ number: 30, author: "outsider" }),
      ],
      { env: ENFORCE, maintainers: ["listed-maintainer", "# a comment"] }
    );
    assert.deepStrictEqual(
      commented.map((c) => c.issue_number),
      [30],
      "only the non-maintainer is commented on"
    );
  }

  // A missing MAINTAINER file must not crash the run (association still applies).
  {
    const github_err = { env: ENFORCE };
    const { commented, warnings } = await run([pr({ number: 31, assoc: "MEMBER" })], github_err);
    assert.strictEqual(commented.length, 0, "MEMBER stays exempt without the file");
    assert.ok(!warnings.some((w) => /throw/i.test(w)));
  }

  console.log("pr-issue-link.test.js: all assertions passed");
})();
