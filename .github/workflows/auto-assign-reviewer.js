// Repo-level reviewer assignment: assign EXACTLY 1 load-balanced reviewer to
// FORK PRs authored by a NON-maintainer, preferring the owners of the area(s)
// the PR touches.
//
// Ownership comes from .github/areas.json (a custom, non-magic path -- NOT
// .github/CODEOWNERS -- so GitHub's native CODEOWNERS auto-request never fires;
// this action is the sole assigner). The candidate pool is the union of owners
// for the PR's changed files; if the PR touches no listed path, it falls back to
// the full set of handles in the file. Maintainers not listed there are never in
// rotation.
//
// An optional prior step may write an LLM area-fit ranking (see
// auto-assign-reviewer.yml); it can only REORDER the candidate pool above (the
// allowlist), and if absent selection is pure load-balancing.
//
// Scope guard: assignment runs only when the PR is from a fork AND the author is
// not in .github/MAINTAINER. Non-fork / collaborator / maintainer PRs are left
// alone (authors pick their own reviewers). Fails closed -- if maintainer status
// can't be determined, it skips rather than risk assigning a maintainer's PR.
//
// "Balance in general": picks are the candidates with the fewest CURRENTLY open
// review requests across the repo (random tie-break) -- stateless fairness.
//
// Only handles drawn from .github/areas.json are ever removed when reconciling,
// so a manually-added reviewer outside that set is left untouched.
//
// Linked-issue sync: the PR's linked ("closes #N") issues are consulted so the
// PR reviewer and the linked-issue assignee stay one and the same person.
//   - If a linked issue is ALREADY assigned to someone in the reviewers pool,
//     that person is adopted as the PR reviewer (overriding the load-balanced
//     area pick) -- "the person who owns the issue reviews the fix".
//   - Whoever ends up the reviewer is then assigned onto any linked issue that
//     has NO assignee yet, so an unowned issue inherits the PR's reviewer.
// Adoption is restricted to the managed reviewers pool (not the wider MAINTAINER
// set) so an adopted reviewer is always removable by the reconcile step -- a
// MAINTAINER not in the pool would be unremovable and could break the "exactly
// 1 reviewer" invariant on a reopen. The push-down direction assigns regardless,
// capped at MAX_PUSHDOWN issues since the fork-author-controlled PR body chooses
// the linked issues. Existing divergences on already-assigned issues are left
// untouched. Needs issues:write (see auto-assign-reviewer.yml) to assign the
// linked issue.
module.exports = async ({ github, context, core }) => {
  const fs = require("fs");
  const TARGET = 1;
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;
  if (!pr || pr.draft) {
    core.info("No PR or draft; nothing to do.");
    return;
  }
  const author = (pr.user && pr.user.login ? pr.user.login : "").toLowerCase();

  // --- Scope guard: fork PRs from non-maintainers only.
  // Precise fork test: the head repo differs from the base repo (head.repo.fork
  // alone means "head repo is a fork of anything", which can false-positive).
  const isFork = !!(
    pr.head && pr.head.repo && pr.base && pr.base.repo &&
    pr.head.repo.full_name !== pr.base.repo.full_name
  );
  if (!isFork) {
    core.info("Not a fork PR; skipping (reviewer auto-assignment is fork-only).");
    return;
  }
  let maint;
  try {
    const m = fs.readFileSync(".github/MAINTAINER", "utf8");
    maint = new Set(
      m.split("\n").map((l) => l.replace(/#.*/, "").trim().toLowerCase()).filter(Boolean)
    );
  } catch (e) {
    // Fail closed: can't verify maintainer status -> don't risk assigning a
    // maintainer-authored PR.
    core.warning("Could not read .github/MAINTAINER; skipping to stay fail-closed.");
    return;
  }
  if (maint.has(author)) {
    core.info(`Author @${author} is a maintainer; skipping (fork PRs from non-maintainers only).`);
    return;
  }

  // --- Parse .github/areas.json into ordered (prefix -> owners) rules + the pool.
  // areas.json is the single source of truth for both this action and issue
  // triage. Each area lists file-prefix `paths` and `owners`; we flatten to one
  // rule per path, preserving document order so "last matching rule wins per
  // file" (below) is controllable -- broad prefixes (e.g. `ap-web/`) are listed
  // before their more-specific children (`ap-web/ios/`). JSON (not YAML) because
  // the github-script sandbox has no YAML parser.
  // REVIEWER_AREAS_FILE lets the unit test pin a frozen fixture so the logic
  // tests don't churn every time real ownership in .github/areas.json changes
  // (areas.test.js validates the real file). Defaults to the real file.
  const areasFile = process.env.REVIEWER_AREAS_FILE || ".github/areas.json";
  const areas = JSON.parse(fs.readFileSync(areasFile, "utf8")).areas;
  const rules = []; // { prefix, owners: [logins] }  (path rules only)
  const poolSet = new Map(); // lc -> original-case
  for (const area of areas) {
    const owners = area.owners || [];
    owners.forEach((o) => poolSet.set(o.toLowerCase(), o));
    for (const p of area.paths || []) {
      // `dir/` or `dir/file_` -> match files whose path startsWith the prefix.
      rules.push({ prefix: p.replace(/^\//, ""), owners });
    }
  }
  const managed = new Set([...poolSet.keys()]); // everyone this action can manage

  // --- Owners of the area(s) this PR touches (last matching rule wins per file,
  // unioned across all changed files).
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

  // --- LLM area-fit ranking (optional, advisory). A trusted prior step
  // (auto-assign-reviewer.yml) may write a ranked list of logins to
  // REVIEWER_RANK_FILE from the area definitions + the changed-file list. It can
  // ONLY reorder the candidate pool computed above -- a login not already a
  // candidate is ignored -- so the LLM can never route a PR to someone who does
  // not own a touched area (the .github/areas.json allowlist). If the file is
  // absent or unparseable (gateway down, no creds, malformed), rankOf is empty
  // and selection falls back to pure load-balancing -- i.e. today's behavior.
  const rank = new Map(); // lc -> 0-based rank (lower = preferred)
  try {
    const rankFile = process.env.REVIEWER_RANK_FILE || "/tmp/reviewer_rank.json";
    const ranked = JSON.parse(fs.readFileSync(rankFile, "utf8"));
    if (Array.isArray(ranked)) {
      ranked.forEach((u, i) => {
        if (typeof u === "string" && !rank.has(u.toLowerCase()))
          rank.set(u.toLowerCase(), i);
      });
      if (rank.size) core.info(`Applying LLM area-fit ranking: [${ranked.join(", ")}]`);
    }
  } catch (e) {
    core.info(`No usable reviewer ranking (${e.code || e.message}); using load only.`);
  }
  const rankOf = (u) => (rank.has(u.toLowerCase()) ? rank.get(u.toLowerCase()) : Infinity);

  // --- Linked ("closes #N") issues for this PR, via GraphQL (the REST PR
  // payload doesn't carry them). Same-repo only. A failure here must not block
  // reviewer assignment, so it degrades to "no linked issues".
  let linkedIssues = []; // [{ number, assignees: [original-case logins] }]
  try {
    const data = await github.graphql(
      `query($owner:String!, $repo:String!, $number:Int!) {
        repository(owner:$owner, name:$repo) {
          pullRequest(number:$number) {
            closingIssuesReferences(first: 20) {
              nodes {
                number
                repository { nameWithOwner }
                assignees(first: 20) { nodes { login } }
              }
            }
          }
        }
      }`,
      { owner, repo, number: pr.number }
    );
    const nodes =
      data?.repository?.pullRequest?.closingIssuesReferences?.nodes || [];
    linkedIssues = nodes
      .filter((n) => n && n.repository?.nameWithOwner === `${owner}/${repo}`)
      .map((n) => ({
        number: n.number,
        assignees: (n.assignees?.nodes || []).map((a) => a.login),
      }));
  } catch (e) {
    core.warning(`Could not read linked issues; proceeding without them: ${e.message}`);
  }

  // Linked-issue assignees who are in the .github/areas.json pool -> adopt as
  // the reviewer. Restricted to the MANAGED pool (not the wider MAINTAINER set)
  // on purpose: an adopted reviewer must be removable by the reconcile step
  // below (which only touches `managed` handles), or a reopened PR could end up
  // with two reviewers -- breaking the "exactly 1" invariant. Pool members are
  // also known area reviewers (collaborators), so adoption can't route a fork PR
  // to an arbitrary or non-collaborator maintainer. A maintainer assigned to the
  // issue but in no area pool falls through to the normal area pick.
  const issueReviewers = [
    ...new Set(linkedIssues.flatMap((li) => li.assignees)),
  ].filter((u) => managed.has(u.toLowerCase()) && u.toLowerCase() !== author);

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

  // Helper: take the N most-preferred from a list. Sort key is (rank, load,
  // random): LLM area-fit rank first (lower = better; Infinity for unranked, so
  // an all-unranked list -- no rank file -- sorts purely by load, i.e. today's
  // behavior), then fewest open review requests, then a pre-rolled random value
  // to break any remaining same-rank-same-load tie. The `!==` guards avoid
  // subtracting two Infinities (which would be NaN).
  const takeLowest = (list, n) => {
    const keyed = list.map((u) => ({ u, r: rankOf(u), l: loadOf(u), j: Math.random() }));
    keyed.sort((a, b) =>
      a.r !== b.r ? a.r - b.r : a.l !== b.l ? a.l - b.l : a.j - b.j
    );
    return keyed.slice(0, n).map((x) => x.u);
  };

  // Desired reviewer. A maintainer already assigned to a linked issue wins
  // (load-balanced if several), so the issue owner reviews the fix. Otherwise
  // fall back to 1 lowest-load area candidate, topped up from the full pool if
  // the area has no eligible owner.
  let desired;
  if (issueReviewers.length) {
    desired = takeLowest(issueReviewers, TARGET);
    core.info(`Adopting linked-issue assignee(s) [${issueReviewers.join(", ")}] as reviewer.`);
  } else {
    desired = takeLowest(candidates, TARGET);
    if (desired.length < TARGET) {
      const have = new Set(desired.map((u) => u.toLowerCase()).concat(author));
      const filler = [...poolSet.values()].filter((u) => !have.has(u.toLowerCase()));
      desired = desired.concat(takeLowest(filler, TARGET - desired.length));
    }
  }
  const desiredLc = new Set(desired.map((u) => u.toLowerCase()));

  // --- Reconcile current requested reviewers to exactly `desired`. Normally
  // nothing is pre-requested, but on a reopened PR (or after a manual add) this
  // keeps the set at the 1 balanced pick.
  const current = (pr.requested_reviewers || []).map((r) => r.login);
  const currentLc = new Set(current.map((c) => c.toLowerCase()));
  const toAdd = desired.filter((u) => !currentLc.has(u.toLowerCase()));
  // Only remove handles this action manages -- never a human added from outside
  // the reviewers file.
  const toRemove = current.filter(
    (u) => managed.has(u.toLowerCase()) && !desiredLc.has(u.toLowerCase())
  );

  if (toAdd.length) {
    // Don't let a failed review request (e.g. a 422 for a non-collaborator)
    // abort the assignee sync + push-down that follow.
    try {
      await github.rest.pulls.requestReviewers({
        owner, repo, pull_number: pr.number, reviewers: toAdd,
      });
    } catch (e) {
      core.warning(`Could not request reviewers [${toAdd.join(", ")}]: ${e.message}`);
    }
  }
  if (toRemove.length) {
    await github.rest.pulls.removeRequestedReviewers({
      owner, repo, pull_number: pr.number, reviewers: toRemove,
    });
  }

  // --- Also sync assignees to mirror the desired reviewer set so PRs are
  // filterable by assignee in the GitHub UI.
  const currentAssignees = (pr.assignees || []).map((a) => a.login);
  const currentAssigneesLc = new Set(currentAssignees.map((a) => a.toLowerCase()));
  const toAddAssignees = desired.filter((u) => !currentAssigneesLc.has(u.toLowerCase()));
  const toRemoveAssignees = currentAssignees.filter(
    (u) => managed.has(u.toLowerCase()) && !desiredLc.has(u.toLowerCase())
  );

  if (toAddAssignees.length) {
    await github.rest.issues.addAssignees({
      owner, repo, issue_number: pr.number, assignees: toAddAssignees,
    });
  }
  if (toRemoveAssignees.length) {
    await github.rest.issues.removeAssignees({
      owner, repo, issue_number: pr.number, assignees: toRemoveAssignees,
    });
  }

  // --- Push-down: mirror the chosen reviewer onto any linked issue that has no
  // assignee yet, so an unowned issue inherits the PR's reviewer. Already-
  // assigned issues are left as-is (existing divergence is tolerated).
  //
  // Bounded by MAX_PUSHDOWN: the PR body is fork-author-controlled, so a PR
  // could list `closes #1..#20` to drive a maintainer onto many issues (bounded,
  // reversible churn -- never an arbitrary user, same-repo only). The norm is one
  // issue per PR, so a small cap blocks the abuse case without affecting real
  // PRs; anything dropped is logged rather than silently skipped.
  const MAX_PUSHDOWN = 5;
  const unassignedLinked = linkedIssues.filter((li) => li.assignees.length === 0);
  if (unassignedLinked.length > MAX_PUSHDOWN) {
    core.warning(
      `${unassignedLinked.length} unassigned linked issues; capping push-down at ` +
        `${MAX_PUSHDOWN}. Skipped: #${unassignedLinked.slice(MAX_PUSHDOWN).map((li) => li.number).join(", #")}.`
    );
  }
  // Per-issue try/catch so one un-assignable issue can't abort the rest.
  const pushedIssues = [];
  if (desired.length) {
    for (const li of unassignedLinked.slice(0, MAX_PUSHDOWN)) {
      try {
        await github.rest.issues.addAssignees({
          owner, repo, issue_number: li.number, assignees: desired,
        });
        pushedIssues.push(li.number);
      } catch (e) {
        core.warning(`Could not assign linked issue #${li.number}: ${e.message}`);
      }
    }
  }

  core.info(
    `Reviewers -> [${desired.join(", ")}]` +
      ` (area pool ${areaOwners.size || "∅→full"}, +${toAdd.length}/-${toRemove.length})` +
      ` | Assignees +${toAddAssignees.length}/-${toRemoveAssignees.length}` +
      ` | Linked issues: ${linkedIssues.length || "none"}` +
      `${issueReviewers.length ? ` (adopted owner)` : ""}` +
      // addAssignees silently ignores users lacking push access, so this is
      // "assignment requested", not a guaranteed landing.
      `${pushedIssues.length ? `, push-down requested on #${pushedIssues.join(", #")}` : ""}.`
  );
};
