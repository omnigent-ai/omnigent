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
async function run({ files, load = {}, current = [], author = "someexternaldev", fork = true }) {
  const listFiles = () => {}; listFiles._tag = "files";
  const list = () => {}; list._tag = "open";
  const added = [], removed = [];
  const github = {
    paginate: async (fn) => (fn._tag === "files"
      ? files.map((f) => ({ filename: f }))
      : mkOpenPRs(load)),
    rest: { pulls: {
      listFiles, list,
      requestReviewers: async ({ reviewers }) => added.push(...reviewers),
      removeRequestedReviewers: async ({ reviewers }) => removed.push(...reviewers),
    } },
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
    } },
  };
  const core = { info: () => {}, warning: (m) => console.log("WARN", m) };
  await script({ github, context, core });
  return { added: added.sort(), removed: removed.sort() };
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

(async () => {
  // 1. inner PR: owners SabhyaC26,TomeHirata,dhruv0811,dbczumar. Loads make the
  //    two lowest deterministic: dhruv0811(0), dbczumar(1) win.
  let r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
  });
  assert("inner picks 2 lowest-load owners", JSON.stringify(r.added) === JSON.stringify(["dbczumar", "dhruv0811"]), JSON.stringify(r));

  // 2. unowned path -> full pool; lowest two by load chosen.
  r = await run({
    files: ["README.md"],
    load: { PattaraS: 9, "serena-ruan": 9, dhruv0811: 9, TomeHirata: 9, SabhyaC26: 9,
            "daniellok-db": 9, hzub: 0, dbczumar: 1, fanzeyi: 9, "ckcuslife-source": 9,
            bbqiu: 9, Edwinhe03: 9 },
  });
  assert("unowned -> 2 lowest from full pool", JSON.stringify(r.added) === JSON.stringify(["dbczumar", "hzub"]), JSON.stringify(r));

  // 3. db has only 2 owners (fanzeyi, SabhyaC26) -> both selected.
  r = await run({ files: ["omnigent/db/x.py"], load: {} });
  assert("db (2 owners) -> both", JSON.stringify(r.added) === JSON.stringify(["SabhyaC26", "fanzeyi"]), JSON.stringify(r));

  // 4. reconcile: all 4 inner owners already requested; keep 2 lowest-load,
  //    remove the other 2.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { SabhyaC26: 5, TomeHirata: 4, dhruv0811: 0, dbczumar: 1 },
    current: ["SabhyaC26", "TomeHirata", "dhruv0811", "dbczumar"],
  });
  assert("reconcile removes the 2 highest-load already-requested",
    JSON.stringify(r.removed) === JSON.stringify(["SabhyaC26", "TomeHirata"]) && r.added.length === 0,
    JSON.stringify(r));

  // 5. mixed current: a managed reviewer not in `desired` is removed, while an
  //    external (unmanaged) reviewer in the same call is preserved.
  r = await run({
    files: ["omnigent/inner/foo.py"],
    load: { dhruv0811: 0, dbczumar: 1, SabhyaC26: 5, TomeHirata: 4 },
    current: ["SabhyaC26", "some-external-human"],
  });
  assert("mixed: managed removed, external preserved",
    r.removed.includes("SabhyaC26") &&
    !r.removed.includes("some-external-human") &&
    JSON.stringify(r.added) === JSON.stringify(["dbczumar", "dhruv0811"]),
    JSON.stringify(r));

  // 6. single-owner area (sandbox -> @SabhyaC26): tops up to 2 from the pool.
  r = await run({
    files: ["omnigent/sandbox/x.py"],
    load: { SabhyaC26: 0, hzub: 0, dhruv0811: 9, dbczumar: 9, TomeHirata: 9, PattaraS: 9,
            "serena-ruan": 9, "daniellok-db": 9, fanzeyi: 9, "ckcuslife-source": 9, bbqiu: 9, Edwinhe03: 9 },
  });
  assert("single-owner area tops up to 2",
    r.added.length === 2 && r.added.includes("SabhyaC26"), JSON.stringify(r));

  // 7. multi-area PR (inner + tools): candidate pool is the UNION; a tools-only
  //    owner (PattaraS) and an inner owner (dhruv0811) can both be picked.
  r = await run({
    files: ["omnigent/inner/a.py", "omnigent/tools/b.py"],
    load: { SabhyaC26: 9, TomeHirata: 9, dbczumar: 9, PattaraS: 0, dhruv0811: 1 },
  });
  assert("multi-area unions both areas' owners",
    r.added.includes("PattaraS") && r.added.includes("dhruv0811") && r.added.length === 2,
    JSON.stringify(r));

  // 8. scope guard: non-fork PR -> nothing assigned.
  r = await run({ files: ["omnigent/inner/foo.py"], fork: false });
  assert("non-fork PR is skipped", r.added.length === 0 && r.removed.length === 0, JSON.stringify(r));

  // 9. scope guard: fork PR authored by a maintainer -> nothing assigned.
  r = await run({ files: ["omnigent/inner/foo.py"], author: "dhruv0811" });
  assert("maintainer-authored fork PR is skipped", r.added.length === 0 && r.removed.length === 0, JSON.stringify(r));
})();
