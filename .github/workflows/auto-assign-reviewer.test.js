// Local unit test for auto-assign-reviewer.js -- mocks the GitHub client and
// runs the real decision logic against the real .github/reviewers and
// .github/MAINTAINER (cwd must be the repo root). No network. Loads are made
// distinct so picks are deterministic.
const path = require("path");
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
async function run({ files, load = {}, current = [], currentAssignees = [], author = "someexternaldev", fork = true }) {
  const listFiles = () => {}; listFiles._tag = "files";
  const list = () => {}; list._tag = "open";
  const added = [], removed = [], assigned = [], unassigned = [];
  const github = {
    paginate: async (fn) => (fn._tag === "files"
      ? files.map((f) => ({ filename: f }))
      : mkOpenPRs(load)),
    rest: {
      pulls: {
        listFiles, list,
        requestReviewers: async ({ reviewers }) => added.push(...reviewers),
        removeRequestedReviewers: async ({ reviewers }) => removed.push(...reviewers),
      },
      issues: {
        addAssignees: async ({ assignees }) => assigned.push(...assignees),
        removeAssignees: async ({ assignees }) => unassigned.push(...assignees),
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: { pull_request: {
      number: 1, draft: false,
      user: { login: author },
      // precise fork detection compares head vs base full_name
      head: { repo: { full_name: fork ? "external-contributor/omnigent" : "omnigent-ai/omnigent" } },
      base: { repo: { full_name: "omnigent-ai/omnigent" } },
      requested_reviewers: current.map((l) => ({ login: l })),
      assignees: currentAssignees.map((l) => ({ login: l })),
    } },
  };
  const core = { info: () => {}, warning: (m) => console.log("WARN", m) };
  await script({ github, context, core });
  return { added: added.sort(), removed: removed.sort(), assigned: assigned.sort(), unassigned: unassigned.sort() };
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
})();
