// Repo-level reviewer assignment: assign EXACTLY 2 load-balanced reviewers to
// FORK PRs authored by a NON-maintainer, preferring the owners of the area(s)
// the PR touches.
//
// Ownership comes from .github/reviewers (a custom, non-magic path -- NOT
// .github/CODEOWNERS -- so GitHub's native CODEOWNERS auto-request never fires;
// this action is the sole assigner). The candidate pool is the union of owners
// for the PR's changed files; if the PR touches no listed path, it falls back to
// the full set of handles in the file. Maintainers not listed there are never in
// rotation.
//
// Scope guard (skipped only for dryRun, which just logs): assignment runs only
// when the PR is from a fork AND the author is not in .github/MAINTAINER --
// non-fork / collaborator / maintainer PRs are left alone (authors pick their
// own reviewers).
//
// "Balance in general": picks are the candidates with the fewest CURRENTLY open
// review requests across the repo (random tie-break) -- stateless fairness.
//
// Only handles drawn from .github/reviewers are ever removed when reconciling,
// so a manually-added reviewer outside that set is left untouched.
//
// `dryRun` (set when the workflow runs on a `pull_request` that edits this
// script) logs the picks instead of mutating reviewers -- a live smoke test.
module.exports = async ({ github, context, core, dryRun = false }) => {
  const fs = require("fs");
  const TARGET = 2;
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;
  if (!pr || pr.draft) {
    core.info("No PR or draft; nothing to do.");
    return;
  }
  const author = (pr.user && pr.user.login ? pr.user.login : "").toLowerCase();

  // --- Scope guard: fork PRs from non-maintainers only. Skipped for dryRun
  // (the smoke test exercises the selection logic on a same-repo PR).
  if (!dryRun) {
    const isFork = !!(pr.head && pr.head.repo && pr.head.repo.fork);
    if (!isFork) {
      core.info("Not a fork PR; skipping (reviewer auto-assignment is fork-only).");
      return;
    }
    let authorIsMaintainer = false;
    try {
      const m = fs.readFileSync(".github/MAINTAINER", "utf8");
      const maint = new Set(
        m.split("\n").map((l) => l.replace(/#.*/, "").trim().toLowerCase()).filter(Boolean)
      );
      authorIsMaintainer = maint.has(author);
    } catch (e) {
      core.warning("Could not read .github/MAINTAINER; proceeding without the maintainer check.");
    }
    if (authorIsMaintainer) {
      core.info(`Author @${author} is a maintainer; skipping (fork PRs from non-maintainers only).`);
      return;
    }
  }

  // --- Parse .github/reviewers into ordered (prefix -> owners) rules + the pool.
  const text = fs.readFileSync(".github/reviewers", "utf8");
  const rules = []; // { prefix, owners: [logins] }  (path rules only)
  const poolSet = new Map(); // lc -> original-case
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line.startsWith("/")) continue;
    const [pat, ...toks] = line.split(/\s+/);
    const owners = toks
      .filter((t) => t.startsWith("@") && !t.includes("/"))
      .map((t) => t.slice(1));
    owners.forEach((o) => poolSet.set(o.toLowerCase(), o));
    // `/dir/` -> match files under `dir/`
    rules.push({ prefix: pat.replace(/^\//, ""), owners });
  }
  const managed = new Set([...poolSet.keys()]); // everyone this action can manage

  // --- Owners of the area(s) this PR touches (last matching rule wins per file).
  const files = await github.paginate(github.rest.pulls.listFiles, {
    owner,
    repo,
    pull_number: pr.number,
    per_page: 100,
  });
  const areaOwners = new Map(); // lc -> original
  for (const f of files) {
    let match = null;
    for (const r of rules) if (f.filename.startsWith(r.prefix)) match = r; // last wins
    if (match) match.owners.forEach((o) => areaOwners.set(o.toLowerCase(), o));
  }

  // Candidates: area owners, else the full pool. Never the author.
  let candidates = [...(areaOwners.size ? areaOwners : poolSet).values()].filter(
    (u) => u.toLowerCase() !== author
  );
  if (candidates.length === 0) {
    core.info("No eligible candidates; nothing to do.");
    return;
  }

  // --- Global open-review load (stateless fairness signal).
  const openPRs = await github.paginate(github.rest.pulls.list, {
    owner,
    repo,
    state: "open",
    per_page: 100,
  });
  const load = new Map();
  for (const p of openPRs)
    for (const r of p.requested_reviewers || []) {
      const l = (r.login || "").toLowerCase();
      load.set(l, (load.get(l) || 0) + 1);
    }
  const loadOf = (u) => load.get(u.toLowerCase()) || 0;

  // Helper: take the N lowest-load from a list, random tie-break within a tier.
  const takeLowest = (list, n) => {
    const byTier = {};
    for (const u of list) (byTier[loadOf(u)] ||= []).push(u);
    const out = [];
    for (const k of Object.keys(byTier).map(Number).sort((a, b) => a - b)) {
      const shuffled = byTier[k]
        .map((v) => [Math.random(), v])
        .sort((a, b) => a[0] - b[0])
        .map(([, v]) => v);
      for (const u of shuffled) if (out.length < n) out.push(u);
      if (out.length >= n) break;
    }
    return out;
  };

  // Desired = 2 lowest-load from candidates; top up from the full pool if an
  // area has fewer than 2 owners.
  let desired = takeLowest(candidates, TARGET);
  if (desired.length < TARGET) {
    const have = new Set(desired.map((u) => u.toLowerCase()).concat(author));
    const filler = [...poolSet.values()].filter((u) => !have.has(u.toLowerCase()));
    desired = desired.concat(takeLowest(filler, TARGET - desired.length));
  }
  const desiredLc = new Set(desired.map((u) => u.toLowerCase()));

  // --- Reconcile current requested reviewers to exactly `desired`. With native
  // CODEOWNERS gone there is normally nothing pre-requested, but on a reopened
  // PR (or after a manual add) this keeps the set at the 2 balanced picks.
  const current = (pr.requested_reviewers || []).map((r) => r.login);
  const currentLc = new Set(current.map((c) => c.toLowerCase()));
  const toAdd = desired.filter((u) => !currentLc.has(u.toLowerCase()));
  // Only remove handles this action manages -- never a human added from outside
  // the reviewers file.
  const toRemove = current.filter(
    (u) => managed.has(u.toLowerCase()) && !desiredLc.has(u.toLowerCase())
  );

  if (toAdd.length && !dryRun) {
    await github.rest.pulls.requestReviewers({
      owner, repo, pull_number: pr.number, reviewers: toAdd,
    });
  }
  if (toRemove.length && !dryRun) {
    await github.rest.pulls.removeRequestedReviewers({
      owner, repo, pull_number: pr.number, reviewers: toRemove,
    });
  }
  core.info(
    `${dryRun ? "[DRY RUN] " : ""}Reviewers -> [${desired.join(", ")}]` +
      ` (area pool ${areaOwners.size || "∅→full"}, +${toAdd.length}/-${toRemove.length}).`
  );
};
