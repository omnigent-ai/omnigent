// Repo-level reviewer assignment: assign EXACTLY 2 load-balanced reviewers per
// PR, preferring the owners of the area(s) the PR touches.
//
// Ownership comes from .github/CODEOWNERS (read at runtime), but GitHub's native
// CODEOWNERS auto-request would request ALL area owners -- we want only 2,
// balanced -- so this action reconciles the request list down to its 2 picks.
// The candidate pool is the union of CODEOWNERS owners for the PR's changed
// files; if the PR touches no owned path, it falls back to the full CODEOWNERS
// pool. Maintainers not listed anywhere in CODEOWNERS are never in rotation.
//
// "Balance in general": picks are the candidates with the fewest CURRENTLY open
// review requests across the repo (random tie-break) -- stateless fairness.
//
// Only handles drawn from CODEOWNERS are ever removed, so a manually-added
// reviewer outside that set is left untouched.
module.exports = async ({ github, context, core }) => {
  const fs = require("fs");
  const TARGET = 2;
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;
  if (!pr || pr.draft) {
    core.info("No PR or draft; nothing to do.");
    return;
  }
  const author = (pr.user && pr.user.login ? pr.user.login : "").toLowerCase();

  // --- Parse CODEOWNERS into ordered (prefix -> owners) rules + the full pool.
  const text = fs.readFileSync(".github/CODEOWNERS", "utf8");
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
  const managed = new Set([...poolSet.keys()]); // everyone CODEOWNERS can manage

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

  // --- Reconcile current requested reviewers to exactly `desired`.
  const current = (pr.requested_reviewers || []).map((r) => r.login);
  const currentLc = new Set(current.map((c) => c.toLowerCase()));
  const toAdd = desired.filter((u) => !currentLc.has(u.toLowerCase()));
  // Only remove CODEOWNERS-managed reviewers we didn't pick -- never humans
  // added from outside the pool.
  const toRemove = current.filter(
    (u) => managed.has(u.toLowerCase()) && !desiredLc.has(u.toLowerCase())
  );

  if (toAdd.length) {
    await github.rest.pulls.requestReviewers({
      owner, repo, pull_number: pr.number, reviewers: toAdd,
    });
  }
  if (toRemove.length) {
    await github.rest.pulls.removeRequestedReviewers({
      owner, repo, pull_number: pr.number, reviewers: toRemove,
    });
  }
  core.info(
    `Reviewers -> [${desired.join(", ")}]` +
      ` (area pool ${areaOwners.size || "∅→full"}, +${toAdd.length}/-${toRemove.length}).`
  );
};
