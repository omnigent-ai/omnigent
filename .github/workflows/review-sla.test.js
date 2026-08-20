// Offline unit test for review-sla.js -- exercises the pure decision helpers and
// one end-to-end orchestration of each path against a mocked GitHub client. No
// network. cwd must be the repo root (the orchestrator reads the real
// .github/MAINTAINER; ownership is pinned to a frozen fixture via
// REVIEWER_AREAS_FILE so the test doesn't churn when .github/areas.json changes).
const path = require("path");
const os = require("os");
const fs = require("fs");
const script = require(path.resolve(".github/workflows/review-sla.js"));

// Frozen area fixture: stable owners the orchestration assertions can pin to.
const FIXTURE = {
  areas: [
    { key: "inner", label: "comp:harnesses", paths: ["omnigent/inner/"], owners: ["ownerA", "ownerB", "ownerC"] },
    { key: "web", label: "comp:web-ui", paths: ["web/"], owners: ["webX", "webY"] },
  ],
};
const FIXTURE_PATH = path.join(os.tmpdir(), "review-sla-areas.fixture.json");
fs.writeFileSync(FIXTURE_PATH, JSON.stringify(FIXTURE));
process.env.REVIEWER_AREAS_FILE = FIXTURE_PATH;

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

const daysAgoIso = (n) => new Date(Date.now() - n * 86400000).toISOString();

// Mocked GitHub client. `canned` maps a list-endpoint tag -> the array it returns
// through github.paginate; writes are recorded in `sink`. `failRequestReviewers`
// makes pulls.requestReviewers throw, to exercise the partial-failure path.
function mkGithub(canned, sink, opts = {}) {
  const list = (tag) => { const f = async () => {}; f._tag = tag; return f; };
  return {
    paginate: async (fn) => canned[fn._tag] || [],
    rest: {
      pulls: {
        list: list("openPRs"),
        listReviews: list("reviews"),
        listReviewComments: list("reviewComments"),
        listFiles: list("files"),
        requestReviewers: async (a) => {
          if (opts.failRequestReviewers) throw new Error("HTTP 422: reviewer is not a collaborator");
          sink.requested.push(...a.reviewers);
        },
      },
      issues: {
        listForRepo: list("openIssues"),
        listEventsForTimeline: list("timeline"),
        listComments: list("comments"),
        createComment: async (a) => sink.comments.push(a),
        addAssignees: async (a) => sink.assigned.push(...a.assignees),
        addLabels: async (a) => sink.labels.push(...a.labels),
      },
    },
  };
}

async function runOrch(canned, opts) {
  const sink = { comments: [], requested: [], assigned: [], labels: [], warnings: [] };
  const core = { info: () => {}, warning: (m) => sink.warnings.push(m) };
  const context = { repo: { owner: "omnigent-ai", repo: "omnigent" } };
  await script({ github: mkGithub(canned, sink, opts), context, core });
  return sink;
}

(async () => {
  // ---- workingDaysBetween (2026-01-05 is a Monday, 01-12 the next Monday) ----
  const wdb = script.workingDaysBetween;
  assert("same day -> 0", wdb("2026-01-05", "2026-01-05") === 0);
  assert("Mon -> next Mon (7 cal days) -> 5 working days", wdb("2026-01-05", "2026-01-12") === 5, String(wdb("2026-01-05", "2026-01-12")));
  assert("Fri -> Mon spans a weekend -> 1", wdb("2026-01-09", "2026-01-12") === 1, String(wdb("2026-01-09", "2026-01-12")));
  assert("Sat -> Sun -> 0", wdb("2026-01-10", "2026-01-11") === 0);

  // ---- latestByUser ----
  const tl = [
    { event: "review_requested", requested_reviewer: { login: "Alice" }, created_at: "2026-01-01T00:00:00Z" },
    { event: "review_requested", requested_reviewer: { login: "Alice" }, created_at: "2026-01-03T00:00:00Z" },
    { event: "assigned", assignee: { login: "Bob" }, created_at: "2026-01-02T00:00:00Z" },
  ];
  const rq = script.latestByUser(tl, "review_requested", (e) => e.requested_reviewer && e.requested_reviewer.login);
  assert("latestByUser keeps the newer event", rq.alice === "2026-01-03T00:00:00Z", JSON.stringify(rq));
  assert("latestByUser ignores other event types", !("bob" in rq));

  // ---- repliedSince ----
  const since = "2026-01-01T00:00:00Z";
  assert("comment after -> replied",
    script.repliedSince("alice", since, [{ user: { login: "Alice" }, created_at: "2026-01-02T00:00:00Z" }], [], []) === true);
  assert("comment before -> not replied",
    script.repliedSince("alice", since, [{ user: { login: "Alice" }, created_at: "2025-12-31T00:00:00Z" }], [], []) === false);
  assert("review after -> replied",
    script.repliedSince("alice", since, [], [{ user: { login: "alice" }, submitted_at: "2026-01-05T00:00:00Z" }], []) === true);
  assert("someone else's comment -> not replied",
    script.repliedSince("alice", since, [{ user: { login: "Bob" }, created_at: "2026-01-09T00:00:00Z" }], [], []) === false);

  // ---- alreadyNudged (marker fallback) ----
  assert("alreadyNudged: marker present -> true", script.alreadyNudged([{ body: "hi " + script.MARKER }]) === true);
  assert("alreadyNudged: no marker -> false", script.alreadyNudged([{ body: "just a normal comment" }]) === false);

  // ---- breachedTargets ----
  const now = new Date();
  const b1 = script.breachedTargets({
    targets: ["Alice"], clockStartByUser: { alice: daysAgoIso(14) }, openedAt: daysAgoIso(30), now,
    comments: [], reviews: [], reviewComments: [],
  });
  assert("stale + silent -> breached", JSON.stringify(b1) === JSON.stringify(["Alice"]), JSON.stringify(b1));
  const b2 = script.breachedTargets({
    targets: ["Alice"], clockStartByUser: { alice: daysAgoIso(1) }, openedAt: daysAgoIso(1), now,
    comments: [], reviews: [], reviewComments: [],
  });
  assert("within SLA -> not breached", b2.length === 0, JSON.stringify(b2));
  const b3 = script.breachedTargets({
    targets: ["Alice"], clockStartByUser: { alice: daysAgoIso(14) }, openedAt: daysAgoIso(30), now,
    comments: [{ user: { login: "Alice" }, created_at: daysAgoIso(1) }], reviews: [], reviewComments: [],
  });
  assert("stale but replied -> not breached", b3.length === 0, JSON.stringify(b3));

  // ---- parseAreas ----
  const { rules, pool, labelOwners } = script.parseAreas(JSON.stringify(FIXTURE));
  assert("parseAreas: rules preserve prefixes", rules.some((r) => r.prefix === "omnigent/inner/") && rules.some((r) => r.prefix === "web/"), JSON.stringify(rules));
  assert("parseAreas: pool unions all owners", ["ownera", "ownerb", "ownerc", "webx", "weby"].every((o) => pool.has(o)), JSON.stringify([...pool.keys()]));
  assert("parseAreas: labelOwners maps comp:* -> owners", [...(labelOwners.get("comp:web-ui") || [])].sort().join(",") === "webX,webY", JSON.stringify([...(labelOwners.get("comp:web-ui") || [])]));

  // ---- pickSecondReviewer ----
  const srMembers = script.pickSecondReviewer({
    files: ["omnigent/inner/foo.py"], rules, pool, load: new Map(),
    exclude: new Set(["ownera"]),
  });
  assert("second reviewer is an inner owner, excluding those on the PR",
    ["ownerb", "ownerc"].includes((srMembers || "").toLowerCase()), String(srMembers));
  const srLoad = script.pickSecondReviewer({
    files: ["omnigent/inner/foo.py"], rules, pool,
    load: new Map([["ownera", 5], ["ownerb", 5], ["ownerc", 0]]),
    exclude: new Set(),
  });
  assert("lowest-load owner wins the tie-break", (srLoad || "").toLowerCase() === "ownerc", String(srLoad));
  const srFallback = script.pickSecondReviewer({
    files: ["README.md"], rules, pool, load: new Map(), exclude: new Set(),
  });
  assert("unowned path -> falls back to the full pool", pool.has((srFallback || "").toLowerCase()), String(srFallback));

  // ---- pickSecondAssignee ----
  const saMatch = script.pickSecondAssignee({
    labels: ["comp:web-ui"], labelOwners, pool, load: new Map(), exclude: new Set(["webx"]),
  });
  assert("second assignee comes from the label's owners, excluding the current one",
    (saMatch || "").toLowerCase() === "weby", String(saMatch));
  const saFallback = script.pickSecondAssignee({
    labels: [], labelOwners, pool, load: new Map(), exclude: new Set(),
  });
  assert("no comp label -> falls back to the full pool", pool.has((saFallback || "").toLowerCase()), String(saFallback));

  // ---- orchestration: a stale, silent PR gets nudged + a 2nd reviewer + label --
  const stalePR = {
    number: 7, draft: false, labels: [], user: { login: "someexternaldev" },
    created_at: daysAgoIso(14), requested_reviewers: [{ login: "dhruv0811" }], assignees: [{ login: "dhruv0811" }],
  };
  let s = await runOrch({
    openPRs: [stalePR], openIssues: [], timeline: [], comments: [], reviews: [], reviewComments: [],
    files: [{ filename: "omnigent/inner/foo.py" }],
  });
  assert("stale PR: one reminder comment posted", s.comments.length === 1 && s.comments[0].issue_number === 7, JSON.stringify(s.comments));
  assert("stale PR: comment re-pings the assigned reviewer", /@dhruv0811/.test(s.comments[0].body), s.comments[0] && s.comments[0].body);
  assert("stale PR: a second reviewer is requested from the area owners",
    s.requested.length === 1 && ["ownera", "ownerb", "ownerc"].includes(s.requested[0].toLowerCase()), JSON.stringify(s.requested));
  assert("stale PR: second reviewer mirrored as assignee", JSON.stringify(s.assigned) === JSON.stringify(s.requested), JSON.stringify(s.assigned));
  assert("stale PR: comment names exactly the reviewer that was added",
    new RegExp(`Adding @${s.requested[0]} as a second reviewer`).test(s.comments[0].body), s.comments[0] && s.comments[0].body);
  assert("stale PR: comment carries the idempotency marker", s.comments[0].body.includes(script.MARKER), s.comments[0] && s.comments[0].body);
  assert("stale PR: labelled once", JSON.stringify(s.labels) === JSON.stringify([script.LABEL]), JSON.stringify(s.labels));

  // ---- orchestration: partial failure -- requestReviewers throws --
  // add-first ordering means the comment must NOT claim a 2nd reviewer that failed
  // to attach, yet the item is still labelled so it won't be re-nudged tomorrow.
  s = await runOrch({
    openPRs: [stalePR], openIssues: [], timeline: [], comments: [], reviews: [], reviewComments: [],
    files: [{ filename: "omnigent/inner/foo.py" }],
  }, { failRequestReviewers: true });
  assert("partial failure: reminder comment still posted", s.comments.length === 1, JSON.stringify(s.comments));
  assert("partial failure: comment does NOT over-claim a second reviewer", !/second reviewer/.test(s.comments[0].body), s.comments[0] && s.comments[0].body);
  assert("partial failure: no reviewer was actually requested", s.requested.length === 0, JSON.stringify(s.requested));
  assert("partial failure: still labelled (won't re-nudge next run)", JSON.stringify(s.labels) === JSON.stringify([script.LABEL]), JSON.stringify(s.labels));
  assert("partial failure: the reviewer-add error is warned, not fatal", s.warnings.some((w) => /could not add second reviewer/.test(w)), JSON.stringify(s.warnings));

  // ---- orchestration: marker fallback -- prior nudge exists but the label didn't --
  s = await runOrch({
    openPRs: [stalePR], openIssues: [], timeline: [], reviews: [], reviewComments: [],
    files: [{ filename: "omnigent/inner/foo.py" }],
    comments: [{ user: { login: "omnigent-ci" }, body: script.MARKER + "\nearlier nudge", created_at: daysAgoIso(2) }],
  });
  assert("marker fallback: an already-nudged PR (marker present, no label) is skipped",
    s.comments.length === 0 && s.labels.length === 0, JSON.stringify(s));

  // ---- orchestration: already-labelled PR is left alone (one-shot) ----
  s = await runOrch({ openPRs: [{ ...stalePR, labels: [{ name: script.LABEL }] }], openIssues: [], files: [] });
  assert("already-escalated PR is skipped", s.comments.length === 0 && s.labels.length === 0, JSON.stringify(s));

  // ---- orchestration: a fresh PR (within SLA) is left alone ----
  s = await runOrch({ openPRs: [{ ...stalePR, created_at: daysAgoIso(1) }], openIssues: [], timeline: [], files: [] });
  assert("fresh PR is not escalated", s.comments.length === 0, JSON.stringify(s));

  // ---- orchestration: a PR whose reviewer already commented is left alone ----
  s = await runOrch({
    openPRs: [stalePR], openIssues: [], timeline: [], reviews: [], reviewComments: [], files: [],
    comments: [{ user: { login: "dhruv0811" }, created_at: daysAgoIso(1) }],
  });
  assert("PR with a recent reply is not escalated", s.comments.length === 0, JSON.stringify(s));

  // ---- orchestration: a stale, silent issue gets nudged + a 2nd assignee + label --
  const staleIssue = {
    number: 9, labels: [{ name: "comp:web-ui" }], created_at: daysAgoIso(14), assignees: [{ login: "hzub" }],
  };
  s = await runOrch({ openPRs: [], openIssues: [staleIssue], timeline: [], comments: [] });
  assert("stale issue: one reminder comment posted", s.comments.length === 1 && s.comments[0].issue_number === 9, JSON.stringify(s.comments));
  assert("stale issue: re-pings the assignee", /@hzub/.test(s.comments[0].body), s.comments[0] && s.comments[0].body);
  assert("stale issue: a second assignee from the label's owners", ["webx", "weby"].includes((s.assigned[0] || "").toLowerCase()), JSON.stringify(s.assigned));
  assert("stale issue: labelled once", JSON.stringify(s.labels) === JSON.stringify([script.LABEL]), JSON.stringify(s.labels));

  // ---- orchestration: a real PR object (listForRepo) is not double-swept as an issue --
  s = await runOrch({ openPRs: [], openIssues: [{ ...staleIssue, pull_request: {} }], timeline: [], comments: [] });
  assert("PR returned by listForRepo is skipped in the issue sweep", s.comments.length === 0, JSON.stringify(s));

  // ---- orchestration: per-run cap + in-sweep load spread ----
  // Feed more stale PRs than the cap. Expect exactly MAX escalations, and the
  // second reviewer rotates across all 3 inner owners rather than dogpiling the
  // one lowest-load maintainer (regression for the live-data concentration bug).
  const MAX = script.MAX_ESCALATIONS_PER_RUN;
  const manyStale = Array.from({ length: MAX + 5 }, (_, i) => ({ ...stalePR, number: 3000 + i }));
  s = await runOrch({
    openPRs: manyStale, openIssues: [], timeline: [], comments: [], reviews: [], reviewComments: [],
    files: [{ filename: "omnigent/inner/foo.py" }],
  });
  assert("cap: escalations stop at MAX_ESCALATIONS_PER_RUN", s.comments.length === MAX, `${s.comments.length} vs ${MAX}`);
  assert("cap: labels capped to match", s.labels.length === MAX, String(s.labels.length));
  assert("load spread: second reviewer rotates across all 3 inner owners (not dogpiled on one)",
    new Set(s.requested.map((u) => u.toLowerCase())).size === 3, JSON.stringify([...new Set(s.requested)]));
})();
