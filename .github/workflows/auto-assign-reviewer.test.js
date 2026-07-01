// Local unit test for auto-assign-reviewer.js -- mocks the GitHub client and
// runs the real decision logic against a FROZEN owner fixture
// (auto-assign-reviewer.fixture.json) + the real .github/MAINTAINER (cwd must be
// the repo root). No network. Loads are made distinct so picks are
// deterministic.
//
// The fixture -- not the live .github/areas.json -- backs these tests on
// purpose: real ownership changes often, and pinning logic assertions to it
// would make them churn/flake. areas.test.js validates the real file instead.
const path = require("path");
const fs = require("fs");
const os = require("os");
// Point the script at the frozen fixture for every run in this file.
process.env.REVIEWER_AREAS_FILE = path.resolve(
  ".github/workflows/auto-assign-reviewer.fixture.json"
);
const script = require(path.resolve(".github/workflows/auto-assign-reviewer.js"));

function mkOpenPRs(loadMap) {
  // one open PR per (reviewer, count) so the script's tally reproduces loadMap
  const prs = [];
  for (const [login, n] of Object.entries(loadMap))
    for (let i = 0; i < n; i++) prs.push({ requested_reviewers: [{ login }] });
  return prs;
}

// author defaults to a non-maintainer; fork defaults to true -- so the scope
// guard passes and the selection logic runs (the cases that assert on picks).
// `linkedIssues` is [{ number, assignees: [logins], repo? }] -- the PR's
// "closes #N" references, served back through the mocked GraphQL endpoint.
async function run({
  files, load = {}, current = [], currentAssignees = [],
  author = "someexternaldev", fork = true, linkedIssues = [],
  rank = null, // LLM area-fit ranking (array of logins) or null for none
}) {
  // Point the script at a per-run rank file so real /tmp state can't leak in.
  // `rank: null` writes no file -> the script's fallback (pure load) is tested,
  // which is what the load-only cases below assert.
  const rankFile = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "rank-")), "reviewer_rank.json"
  );
  if (rank) fs.writeFileSync(rankFile, JSON.stringify(rank));
  process.env.REVIEWER_RANK_FILE = rankFile;
  const listFiles = () => {}; listFiles._tag = "files";
  const list = () => {}; list._tag = "open";
  const PR_NUMBER = 1;
  const added = [], removed = [], unassigned = [];
  // PR-assignee changes (issue_number === PR) vs linked-issue assignments are
  // tracked separately so tests can assert the push-down direction in isolation.
  const assigned = [];                 // assignees added to the PR itself
  const issueAssigned = {};            // { issueNumber: [logins] } for linked issues
  const github = {
    paginate: async (fn) => (fn._tag === "files"
      ? files.map((f) => ({ filename: f }))
      : mkOpenPRs(load)),
    graphql: async () => ({
      repository: {
        pullRequest: {
          closingIssuesReferences: {
            nodes: linkedIssues.map((li) => ({
              number: li.number,
              repository: { nameWithOwner: li.repo || "omnigent-ai/omnigent" },
              assignees: { nodes: (li.assignees || []).map((login) => ({ login })) },
            })),
          },
        },
      },
    }),
    rest: {
      pulls: {
        listFiles, list,
        requestReviewers: async ({ reviewers }) => added.push(...reviewers),
        removeRequestedReviewers: async ({ reviewers }) => removed.push(...reviewers),
      },
      issues: {
        addAssignees: async ({ issue_number, assignees }) => {
          if (issue_number === PR_NUMBER) assigned.push(...assignees);
          else (issueAssigned[issue_number] ||= []).push(...assignees);
        },
        removeAssignees: async ({ assignees }) => unassigned.push(...assignees),
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: { pull_request: {
      number: PR_NUMBER, draft: false,
      user: { login: author },
      // precise fork detection compares head vs base full_name
      head: { repo: { full_name: fork ? "external-contributor/omnigent" : "omnigent-ai/omnigent" } },
      base: { repo: { full_name: "omnigent-ai/omnigent" } },
      requested_reviewers: current.map((l) => ({ login: l })),
      assignees: currentAssignees.map((l) => ({ login: l })),
    } },
  };
  const warnings = [];
  const core = { info: () => {}, warning: (m) => warnings.push(m) };
  await script({ github, context, core });
  return {
    added: added.sort(), removed: removed.sort(),
    assigned: assigned.sort(), unassigned: unassigned.sort(),
    issueAssigned, warnings,
  };
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

(async () => {
  // 1. inner PR: owners SabhyaC26,TomeHirata,dhruv0811,dbczumar. Loads make the
  //    single lowest deterministic: dhruv0811(0) wins.
  let r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
  });
  assert("inner picks the lowest-load owner", JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));
  assert("inner: reviewer also added as assignee", JSON.stringify(r.assigned) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));

  // 2. unowned path -> full pool; lowest by load chosen.
  r = await run({
    files: ["README.md"],
    load: { PattaraS: 9, "serena-ruan": 9, dhruv0811: 9, TomeHirata: 9, SabhyaC26: 9,
            "daniellok-db": 9, dbczumar: 0, fanzeyi: 9, "ckcuslife-source": 9,
            bbqiu: 9, Edwinhe03: 9 },
  });
  assert("unowned -> lowest from full pool", JSON.stringify(r.added) === JSON.stringify(["dbczumar"]), JSON.stringify(r));

  // 3. db area (fanzeyi, SabhyaC26) -> the lower-load one selected.
  r = await run({ files: ["omnigent/db/x.py"], load: { SabhyaC26: 1 } });
  assert("db -> lowest-load owner", JSON.stringify(r.added) === JSON.stringify(["fanzeyi"]), JSON.stringify(r));

  // 4. reconcile: all 4 inner owners already requested; keep the lowest-load,
  //    remove the other 3.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    current: ["SabhyaC26", "TomeHirata", "dhruv0811", "dbczumar"],
    currentAssignees: ["SabhyaC26", "TomeHirata", "dhruv0811", "dbczumar"],
  });
  assert("reconcile removes the 3 higher-load already-requested",
    JSON.stringify(r.removed) === JSON.stringify(["SabhyaC26", "TomeHirata", "dbczumar"]) && r.added.length === 0,
    JSON.stringify(r));
  assert("reconcile: removes the 3 stale assignees, keeps dhruv0811",
    JSON.stringify(r.unassigned) === JSON.stringify(["SabhyaC26", "TomeHirata", "dbczumar"]) && r.assigned.length === 0,
    JSON.stringify(r));

  // 5. mixed current: a managed reviewer not in `desired` is removed, while an
  //    external (unmanaged) reviewer in the same call is preserved.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { dhruv0811: 0, dbczumar: 1, SabhyaC26: 5, TomeHirata: 4 },
    current: ["SabhyaC26", "some-external-human"],
    currentAssignees: ["SabhyaC26", "some-external-human"],
  });
  assert("mixed: managed removed, external preserved",
    r.removed.includes("SabhyaC26") &&
    !r.removed.includes("some-external-human") &&
    JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]),
    JSON.stringify(r));
  assert("mixed: new reviewer assigned, stale managed assignee removed, external assignee preserved",
    JSON.stringify(r.assigned) === JSON.stringify(["dhruv0811"]) &&
    r.unassigned.includes("SabhyaC26") &&
    !r.unassigned.includes("some-external-human"),
    JSON.stringify(r));

  // 6. single-owner area (sandbox -> @SabhyaC26): the lone owner is selected.
  r = await run({
    files: ["omnigent/sandbox/x.py"],
    load: { SabhyaC26: 0, hzub: 0, dhruv0811: 9, dbczumar: 9, TomeHirata: 9, PattaraS: 9,
            "serena-ruan": 9, "daniellok-db": 9, fanzeyi: 9, "ckcuslife-source": 9, bbqiu: 9, Edwinhe03: 9 },
  });
  assert("single-owner area picks that owner",
    JSON.stringify(r.added) === JSON.stringify(["SabhyaC26"]), JSON.stringify(r));

  // 7. multi-area PR (inner + tools): candidate pool is the UNION; the lowest-load
  //    across both areas wins -- here a tools-only owner (PattaraS).
  r = await run({
    files: ["omnigent/inner/a.py", "omnigent/tools/b.py"],
    load: { SabhyaC26: 9, TomeHirata: 9, dbczumar: 9, PattaraS: 0, dhruv0811: 1 },
  });
  assert("multi-area unions both areas' owners",
    JSON.stringify(r.added) === JSON.stringify(["PattaraS"]),
    JSON.stringify(r));

  // 8. scope guard: non-fork PR -> nothing assigned.
  r = await run({ files: ["omnigent/inner/foo.py"], fork: false });
  assert("non-fork PR is skipped", r.added.length === 0 && r.removed.length === 0, JSON.stringify(r));

  // 9. scope guard: fork PR authored by a maintainer -> nothing assigned.
  r = await run({ files: ["omnigent/inner/foo.py"], author: "dhruv0811" });
  assert("maintainer-authored fork PR is skipped", r.added.length === 0 && r.removed.length === 0, JSON.stringify(r));

  // 10. linked issue ALREADY assigned to a maintainer -> adopted as reviewer,
  //     overriding the area pick (dhruv0811 would otherwise win on load here).
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [{ number: 42, assignees: ["TomeHirata"] }],
  });
  assert("linked-issue maintainer assignee is adopted as reviewer",
    JSON.stringify(r.added) === JSON.stringify(["TomeHirata"]), JSON.stringify(r));
  assert("adopted reviewer also mirrored onto the PR assignees",
    JSON.stringify(r.assigned) === JSON.stringify(["TomeHirata"]), JSON.stringify(r));
  assert("already-assigned linked issue is NOT re-assigned",
    Object.keys(r.issueAssigned).length === 0, JSON.stringify(r.issueAssigned));

  // 11. linked issue with NO assignee -> normal area pick, then pushed down onto
  //     the issue so it inherits the PR's reviewer.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [{ number: 77, assignees: [] }],
  });
  assert("unassigned linked issue: reviewer is the area pick",
    JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));
  assert("unassigned linked issue inherits the chosen reviewer",
    JSON.stringify(r.issueAssigned[77]) === JSON.stringify(["dhruv0811"]), JSON.stringify(r.issueAssigned));

  // 12. linked issue assigned to a NON-maintainer -> not adopted (area pick
  //     stands) and not re-assigned (it already has an assignee).
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [{ number: 88, assignees: ["someexternaldev"] }],
  });
  assert("non-maintainer issue assignee is NOT adopted as reviewer",
    JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));
  assert("issue with a (non-maintainer) assignee is left untouched",
    Object.keys(r.issueAssigned).length === 0, JSON.stringify(r.issueAssigned));

  // 13. two linked issues -- one assigned to a maintainer, one unassigned: the
  //     maintainer is adopted AND mirrored onto the unassigned sibling.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [
      { number: 10, assignees: ["TomeHirata"] },
      { number: 11, assignees: [] },
    ],
  });
  assert("two issues: maintainer adopted as reviewer",
    JSON.stringify(r.added) === JSON.stringify(["TomeHirata"]), JSON.stringify(r));
  assert("two issues: unassigned sibling inherits the same reviewer",
    JSON.stringify(r.issueAssigned[11]) === JSON.stringify(["TomeHirata"]) &&
    !(10 in r.issueAssigned), JSON.stringify(r.issueAssigned));

  // 14. cross-repo linked issue is ignored (different nameWithOwner).
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [{ number: 99, assignees: ["TomeHirata"], repo: "other-org/other-repo" }],
  });
  assert("cross-repo linked issue does not affect the reviewer pick",
    JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));
  assert("cross-repo linked issue is not assigned",
    Object.keys(r.issueAssigned).length === 0, JSON.stringify(r.issueAssigned));

  // 15. linked issue assigned to a maintainer who is NOT in the reviewers pool
  //     (hzub is in .github/MAINTAINER but not .github/areas.json): NOT adopted
  //     (adoption is restricted to the managed pool so the reviewer stays
  //     removable), so the normal area pick stands. The issue already has an
  //     assignee, so no push-down.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: [{ number: 55, assignees: ["hzub"] }],
  });
  assert("non-pool maintainer issue assignee is NOT adopted as reviewer",
    JSON.stringify(r.added) === JSON.stringify(["dhruv0811"]), JSON.stringify(r));
  assert("non-pool maintainer issue is left untouched",
    Object.keys(r.issueAssigned).length === 0, JSON.stringify(r.issueAssigned));

  // 16. push-down is capped: 7 unassigned linked issues -> only MAX_PUSHDOWN (5)
  //     get the reviewer; the overflow is logged, not silently dropped.
  const manyIssues = [201, 202, 203, 204, 205, 206, 207].map((n) => ({ number: n, assignees: [] }));
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    linkedIssues: manyIssues,
  });
  assert("push-down capped at 5 issues",
    Object.keys(r.issueAssigned).length === 5, JSON.stringify(Object.keys(r.issueAssigned)));
  assert("capped overflow is warned",
    r.warnings.some((w) => /capping push-down/.test(w)), JSON.stringify(r.warnings));

  // 17. LLM ranking overrides load within the candidate pool: dhruv0811 has the
  //     lowest load (would win on load alone), but the rank prefers dbczumar, an
  //     inner owner -- so dbczumar is chosen.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    rank: ["dbczumar", "TomeHirata", "SabhyaC26", "dhruv0811"],
  });
  assert("LLM rank beats load within the area pool",
    JSON.stringify(r.added) === JSON.stringify(["dbczumar"]), JSON.stringify(r));

  // 18. Allowlist enforcement: a rank naming someone who does NOT own the touched
  //     area (PattaraS is a maintainer + pool member, but not an inner owner) is
  //     ignored for that entry; the ranking only reorders actual candidates, so
  //     the next ranked inner owner (dbczumar) wins -- never PattaraS.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1, PattaraS: 0 },
    rank: ["PattaraS", "dbczumar", "TomeHirata", "SabhyaC26", "dhruv0811"],
  });
  assert("LLM rank cannot route outside the area owners",
    JSON.stringify(r.added) === JSON.stringify(["dbczumar"]) && !r.added.includes("PattaraS"),
    JSON.stringify(r));

  // 19. Unranked candidates (rank omits them) sort after ranked ones but still by
  //     load: rank lists only SabhyaC26 (highest load); the rest are unranked, so
  //     SabhyaC26 -- despite load 5 -- is preferred because a finite rank beats
  //     Infinity. Confirms the rank-primary / load-secondary ordering.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    rank: ["SabhyaC26"],
  });
  assert("a ranked high-load owner beats unranked low-load owners",
    JSON.stringify(r.added) === JSON.stringify(["SabhyaC26"]), JSON.stringify(r));

  // 20. Adoption still overrides the LLM rank: a linked-issue maintainer assignee
  //     (TomeHirata) is adopted as reviewer even when the rank prefers someone
  //     else -- the issue owner reviews the fix.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    rank: ["dbczumar", "dhruv0811"],
    linkedIssues: [{ number: 42, assignees: ["TomeHirata"] }],
  });
  assert("linked-issue adoption overrides the LLM rank",
    JSON.stringify(r.added) === JSON.stringify(["TomeHirata"]), JSON.stringify(r));
})();
