// Reviewer SLA sweep: nudge + escalate open PRs and issues that a MAINTAINER has
// been sitting on for more than SLA_DAYS *working* days without replying.
//
// Runs on a schedule from the trusted default branch (see review-sla.yml), so it
// reads no PR-authored code and just talks to the issues/PRs API. For each open,
// non-draft item:
//   - PRs: the "assigned person" is any maintainer in requested_reviewers (GitHub
//     drops them from that list the moment they submit a review, so being in it
//     means "still owes a review"). The clock starts at their latest
//     `review_requested` event (fallback: PR opened). If >= SLA_DAYS working days
//     have elapsed AND they've posted no comment or review since, the SLA is
//     breached: re-ping them in one comment and add ONE second reviewer (lowest
//     open-review load among the area owners in .github/areas.json, mirrored as
//     an assignee like auto-assign-reviewer.js does).
//   - Issues: the "assigned person" is any maintainer assignee; clock starts at
//     their latest `assigned` event. Breach -> re-ping + add one second assignee
//     from the owners of the area(s) whose comp:* label the issue carries.
//
// Ownership comes from .github/areas.json -- the single source of truth shared
// with auto-assign-reviewer.js and issue-triage.yml (it replaced the old
// .github/reviewers + .github/ISSUE_ASSIGNEES files). `owners_paused` is ignored.
//
// "Working days" = weekdays (Mon-Fri) in UTC. Reply = ANY comment or review by the
// assignee since the clock started.
//
// Escalate-once, two independent guards so the bot never spams:
//   1. a one-shot LABEL, and
//   2. the MARKER hidden in the reminder comment -- checked as a fallback so that
//      even if the label write fails after the comment lands, the next sweep still
//      sees the marker and skips.
// The second reviewer/assignee is added FIRST (best-effort); the comment is then
// worded to match what actually happened (so it can't claim "Adding @X" when the
// add 422'd), and the label is written last. If the comment itself fails nothing
// user-visible was posted, so we skip the label and let the next sweep retry.
//
// ponytail: one escalation per item. Per-reviewer re-escalation or a weekly
// re-ping would need per-nudge timestamp state instead of the label+marker pair --
// add that only if a single nudge proves too weak.

const fs = require("fs");

const SLA_DAYS = 5; // working days
const LABEL = "review-sla-escalated";
const MARKER = "<!-- review-sla-bot -->"; // idempotency fallback if the label write fails
const CANONICAL_REPO = "omnigent-ai/omnigent";
// Max escalations per sweep. Bounds the day-one blast against an existing stale
// backlog (and any future surge): the backlog drains a chunk per weekday instead
// of nudging everything at once. PRs are processed before issues.
// ponytail: single global cap; split into per-kind caps if issue nudges starving
// behind a large PR backlog ever matters.
const MAX_ESCALATIONS_PER_RUN = 30;

// --- Pure helpers (exported for the offline test; no network) --------------

// Weekdays strictly after `from`'s date, through `to`'s date, in UTC. So a review
// requested on a Monday first counts as 5 working days the following Monday.
// ponytail: weekends only, no holiday calendar -- add one if the SLA needs it.
function workingDaysBetween(from, to) {
  const cur = new Date(from);
  cur.setUTCHours(0, 0, 0, 0);
  const end = new Date(to);
  end.setUTCHours(0, 0, 0, 0);
  let count = 0;
  while (cur < end) {
    cur.setUTCDate(cur.getUTCDate() + 1);
    const d = cur.getUTCDay();
    if (d !== 0 && d !== 6) count++;
  }
  return count;
}

// Latest ISO timestamp per (lowercased) login for a given timeline event type.
function latestByUser(timeline, eventName, getLogin) {
  const out = {};
  for (const e of timeline || []) {
    if (e.event !== eventName) continue;
    const login = getLogin(e);
    if (!login || !e.created_at) continue;
    const lc = login.toLowerCase();
    if (!out[lc] || new Date(e.created_at) > new Date(out[lc])) out[lc] = e.created_at;
  }
  return out;
}

// Did `login` post any comment/review after `sinceIso`?
function repliedSince(login, sinceIso, comments, reviews, reviewComments) {
  const since = new Date(sinceIso).getTime();
  const lc = login.toLowerCase();
  const by = (u) => (u || "").toLowerCase() === lc;
  const after = (t) => t && new Date(t).getTime() > since;
  return (
    (comments || []).some((c) => by(c.user && c.user.login) && after(c.created_at)) ||
    (reviews || []).some((r) => by(r.user && r.user.login) && after(r.submitted_at)) ||
    (reviewComments || []).some((rc) => by(rc.user && rc.user.login) && after(rc.created_at))
  );
}

// Have we already posted a reminder here? (idempotency fallback for a failed label)
function alreadyNudged(comments) {
  return (comments || []).some((c) => (c.body || "").includes(MARKER));
}

// Breached maintainer targets for one item, given the reply signals. Shared by the
// PR and issue paths (issues pass [] for reviews/reviewComments).
function breachedTargets({ targets, clockStartByUser, openedAt, now, comments, reviews, reviewComments }) {
  const out = [];
  for (const t of targets) {
    // Fallback to openedAt when there's no explicit request/assign event for
    // this login (e.g. a CODEOWNERS/team expansion, or a timeline pagination
    // edge). That can over-count elapsed time slightly -- acceptable, and never
    // fires for the normal auto-assigned path which always emits the event.
    const since = clockStartByUser[t.toLowerCase()] || openedAt;
    if (workingDaysBetween(since, now) < SLA_DAYS) continue;
    if (repliedSince(t, since, comments, reviews, reviewComments)) continue;
    out.push(t);
  }
  return out;
}

// Parse .github/areas.json (same shape auto-assign-reviewer.js reads) into:
//   rules       - [{ prefix, owners }] in document order (last match wins per file)
//   pool        - Map lc->original of every owner (the full candidate set)
//   labelOwners - Map "comp:x" -> Set of owners, for routing an issue by its label
// `owners_paused` is intentionally ignored. `text` is injectable for tests.
function parseAreas(text) {
  const areas = JSON.parse(text).areas || [];
  const rules = [];
  const pool = new Map();
  const labelOwners = new Map();
  for (const area of areas) {
    const owners = area.owners || [];
    owners.forEach((o) => pool.set(o.toLowerCase(), o));
    for (const p of area.paths || []) rules.push({ prefix: p.replace(/^\//, ""), owners });
    if (area.label) {
      const set = labelOwners.get(area.label) || new Set();
      owners.forEach((o) => set.add(o));
      labelOwners.set(area.label, set);
    }
  }
  return { rules, pool, labelOwners };
}

// Count currently-open review requests per (lc) login -- the stateless fairness
// signal auto-assign-reviewer.js also uses.
function buildLoad(openPRs) {
  const load = new Map();
  for (const p of openPRs)
    for (const r of p.requested_reviewers || []) {
      const l = (r.login || "").toLowerCase();
      load.set(l, (load.get(l) || 0) + 1);
    }
  return load;
}

// Pick the lowest-load of a candidate list, random tie-break within a load tier.
function lowestLoad(candidates, load) {
  if (!candidates.length) return null;
  const loadOf = (u) => load.get(u.toLowerCase()) || 0;
  const byTier = {};
  for (const u of candidates) (byTier[loadOf(u)] ||= []).push(u);
  const lowest = byTier[Math.min(...Object.keys(byTier).map(Number))];
  return lowest[Math.floor(Math.random() * lowest.length)];
}

// One lowest-load area owner for the PR's files, else lowest from the full pool;
// never anyone already on the PR.
function pickSecondReviewer({ files, rules, pool, load, exclude }) {
  const areaOwners = new Map();
  for (const f of files) {
    let match = null;
    for (const r of rules) if (f.startsWith(r.prefix)) match = r; // last wins
    if (match) match.owners.forEach((o) => areaOwners.set(o.toLowerCase(), o));
  }
  const base = areaOwners.size ? areaOwners : pool;
  return lowestLoad([...base.values()].filter((u) => !exclude.has(u.toLowerCase())), load);
}

// One second assignee from the owners of the issue's comp:* area(s), else the full
// pool; never anyone already assigned.
// ponytail: tie-break reuses the PR open-review `load` -- a proxy for issues (there
// is no per-assignee open-issue count), so this only approximates issue fairness.
// Tally open-issue assignee counts here if that starts to matter.
function pickSecondAssignee({ labels, labelOwners, pool, load, exclude }) {
  const owners = new Set();
  for (const l of labels) for (const o of labelOwners.get(l) || []) owners.add(o);
  const base = owners.size ? owners : new Set(pool.values());
  return lowestLoad([...base].filter((u) => !exclude.has(u.toLowerCase())), load);
}

// --- Orchestrator ----------------------------------------------------------

async function run({ github, context, core }) {
  const { owner, repo } = context.repo;
  if (`${owner}/${repo}` !== CANONICAL_REPO) {
    core.info(`Not ${CANONICAL_REPO}; skipping.`);
    return;
  }
  const now = new Date();

  const maintainers = new Set(
    fs.readFileSync(".github/MAINTAINER", "utf8")
      .split("\n").map((l) => l.replace(/#.*/, "").trim().toLowerCase()).filter(Boolean)
  );
  // REVIEWER_AREAS_FILE lets the unit test pin a fixture; defaults to the real file.
  const areasFile = process.env.REVIEWER_AREAS_FILE || ".github/areas.json";
  const { rules, pool, labelOwners } = parseAreas(fs.readFileSync(areasFile, "utf8"));

  const hasLabel = (item) => (item.labels || []).some((l) => (l.name || l) === LABEL);
  const escalated = [];
  const capReached = () => escalated.length >= MAX_ESCALATIONS_PER_RUN;

  // Escalate one item once. Add the second reviewer/assignee FIRST (best-effort,
  // returns the login it actually added or null), so the comment states the true
  // outcome; then post the marked comment; then lock the LABEL. If the comment
  // fails, nothing was posted -> skip the label and retry next sweep.
  const escalateOnce = async (number, breached, kind, addSecond, secondCandidate) => {
    let added = null;
    if (secondCandidate) {
      try {
        added = (await addSecond()) ? secondCandidate : null;
      } catch (e) {
        core.warning(`#${number}: could not add second ${kind} @${secondCandidate}: ${e.message}`);
      }
    }
    const noun = kind === "reviewer" ? "review" : "a response";
    const body =
      `${MARKER}\n⏰ **${kind === "reviewer" ? "Reviewer" : "Response"} SLA** — this ${kind === "reviewer" ? "PR" : "issue"} ` +
      `has been awaiting ${noun} from ${breached.map((u) => "@" + u).join(", ")} for more than ${SLA_DAYS} working days.` +
      (added ? ` Adding @${added} as a second ${kind}.` : "");
    try {
      await github.rest.issues.createComment({ owner, repo, issue_number: number, body });
    } catch (e) {
      core.warning(`#${number}: reminder comment failed, will retry next run: ${e.message}`);
      return;
    }
    try {
      await github.rest.issues.addLabels({ owner, repo, issue_number: number, labels: [LABEL] });
    } catch (e) {
      core.warning(`#${number}: could not add ${LABEL} label (marker still guards re-nudge): ${e.message}`);
    }
    escalated.push(`${kind === "reviewer" ? "PR" : "issue"} #${number} (re-pinged ${breached.join(", ")}${added ? `, +@${added}` : ""})`);
  };

  // ----- PRs: awaiting a maintainer's review -----
  const openPRs = await github.paginate(github.rest.pulls.list, { owner, repo, state: "open", per_page: 100 });
  const load = buildLoad(openPRs);
  // Count each second reviewer/assignee we add during THIS sweep against the load
  // map, so successive picks rotate instead of dogpiling the current lowest-load
  // maintainer -- without it, one sweep hands nearly every escalation to one person.
  const bumpLoad = (u) => load.set(u.toLowerCase(), (load.get(u.toLowerCase()) || 0) + 1);

  for (const pr of openPRs) {
    if (capReached()) break;
    if (pr.draft || hasLabel(pr)) continue;
    const targets = (pr.requested_reviewers || []).map((r) => r.login).filter((l) => maintainers.has(l.toLowerCase()));
    if (!targets.length) continue;

    const timeline = await github.paginate(github.rest.issues.listEventsForTimeline, { owner, repo, issue_number: pr.number, per_page: 100 });
    const requestedAt = latestByUser(timeline, "review_requested", (e) => e.requested_reviewer && e.requested_reviewer.login);

    // Cheap staleness prefilter before fetching reply signals.
    const stale = targets.filter((t) => workingDaysBetween(requestedAt[t.toLowerCase()] || pr.created_at, now) >= SLA_DAYS);
    if (!stale.length) continue;

    const [comments, reviews, reviewComments] = await Promise.all([
      github.paginate(github.rest.issues.listComments, { owner, repo, issue_number: pr.number, per_page: 100 }),
      github.paginate(github.rest.pulls.listReviews, { owner, repo, pull_number: pr.number, per_page: 100 }),
      github.paginate(github.rest.pulls.listReviewComments, { owner, repo, pull_number: pr.number, per_page: 100 }),
    ]);
    if (alreadyNudged(comments)) continue; // label may have failed to write; marker still guards
    const breached = breachedTargets({
      targets: stale, clockStartByUser: requestedAt, openedAt: pr.created_at, now, comments, reviews, reviewComments,
    });
    if (!breached.length) continue;

    const files = (await github.paginate(github.rest.pulls.listFiles, { owner, repo, pull_number: pr.number, per_page: 100 })).map((f) => f.filename);
    const onPr = new Set(
      [pr.user && pr.user.login, ...targets, ...(pr.assignees || []).map((a) => a.login), ...(pr.requested_reviewers || []).map((r) => r.login)]
        .filter(Boolean).map((s) => s.toLowerCase())
    );
    const second = pickSecondReviewer({ files, rules, pool, load, exclude: onPr });

    await escalateOnce(pr.number, breached, "reviewer", async () => {
      await github.rest.pulls.requestReviewers({ owner, repo, pull_number: pr.number, reviewers: [second] });
      // Mirror as assignee for UI filterability, matching auto-assign-reviewer.js.
      await github.rest.issues.addAssignees({ owner, repo, issue_number: pr.number, assignees: [second] });
      bumpLoad(second);
      return true;
    }, second);
  }

  // ----- Issues: awaiting a maintainer assignee -----
  const openIssues = await github.paginate(github.rest.issues.listForRepo, { owner, repo, state: "open", per_page: 100 });
  for (const issue of openIssues) {
    if (capReached()) break;
    if (issue.pull_request || hasLabel(issue)) continue; // listForRepo also returns PRs
    const targets = (issue.assignees || []).map((a) => a.login).filter((l) => maintainers.has(l.toLowerCase()));
    if (!targets.length) continue;

    const timeline = await github.paginate(github.rest.issues.listEventsForTimeline, { owner, repo, issue_number: issue.number, per_page: 100 });
    const assignedAt = latestByUser(timeline, "assigned", (e) => e.assignee && e.assignee.login);

    const stale = targets.filter((t) => workingDaysBetween(assignedAt[t.toLowerCase()] || issue.created_at, now) >= SLA_DAYS);
    if (!stale.length) continue;

    const comments = await github.paginate(github.rest.issues.listComments, { owner, repo, issue_number: issue.number, per_page: 100 });
    if (alreadyNudged(comments)) continue;
    const breached = breachedTargets({
      targets: stale, clockStartByUser: assignedAt, openedAt: issue.created_at, now, comments, reviews: [], reviewComments: [],
    });
    if (!breached.length) continue;

    const labels = (issue.labels || []).map((l) => l.name || l).filter((n) => n.startsWith("comp:"));
    const onIssue = new Set((issue.assignees || []).map((a) => a.login.toLowerCase()));
    const second = pickSecondAssignee({ labels, labelOwners, pool, load, exclude: onIssue });

    await escalateOnce(issue.number, breached, "assignee", async () => {
      await github.rest.issues.addAssignees({ owner, repo, issue_number: issue.number, assignees: [second] });
      bumpLoad(second);
      return true;
    }, second);
  }

  core.info(escalated.length ? `Escalated ${escalated.length}: ${escalated.join("; ")}.` : "No SLA breaches; nothing to escalate.");
}

module.exports = run;
// Exported for the offline unit test.
module.exports.workingDaysBetween = workingDaysBetween;
module.exports.latestByUser = latestByUser;
module.exports.repliedSince = repliedSince;
module.exports.alreadyNudged = alreadyNudged;
module.exports.breachedTargets = breachedTargets;
module.exports.parseAreas = parseAreas;
module.exports.pickSecondReviewer = pickSecondReviewer;
module.exports.pickSecondAssignee = pickSecondAssignee;
module.exports.SLA_DAYS = SLA_DAYS;
module.exports.LABEL = LABEL;
module.exports.MARKER = MARKER;
module.exports.MAX_ESCALATIONS_PER_RUN = MAX_ESCALATIONS_PER_RUN;
