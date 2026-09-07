// Scan PRs opened in the last 24 hours and flag any that don't link an issue.
// Runs hourly from the demo-check sweep; the 24-hour window ensures every new PR
// is checked even if it was opened just before a cron tick.
//
// A flagged PR gets one comment and the `needs-issue` label. The label is the
// whole state machine: it dedupes the comment, it is how a later sweep finds the
// PR again (the 24-hour window cannot, and GitHub search cannot match a marker
// hidden in a comment body), and its application time is the clock. A PR still
// unlinked CLOSE_AFTER_DAYS after the label went on is closed, reversibly, with a
// pointer at `/reopen`.
//
// The label is derived state, recomputed every run: a PR that should be flagged
// gets it, a PR that should not has it removed. So linking the issue, ticking a
// chore box, or converting to draft all clear it, and a PR that becomes flaggable
// again starts a fresh clock rather than inheriting a stale one.
//
// Forward-only: nothing opened before EFFECTIVE_FROM is ever considered, so the
// existing backlog is untouched no matter how the scan window is set.
//
// ENFORCE=false (the default) is a dry run: it resolves every verdict and writes
// them to the step summary without commenting or labeling.
//
// Exemptions, in the order applied:
//   - bots (release automation can't file issues; our CI bots author as
//     CONTRIBUTOR, not MEMBER, so association checks miss them)
//   - drafts
//   - an affirmatively checked `Refactor / chore`, `Docs`, or `Test / CI` box,
//     with no `Bug fix` / `Feature` / `UI` box also checked. Note this requires a
//     DECLARATION: an empty or deleted template does NOT exempt, or removing the
//     template would become the way to skip the rule.
//   - trivial changes (<= 9 changed lines, the size/XS cutoff) -- Spark's
//     "trivial changes ... do not require a JIRA". Counts raw additions +
//     deletions, so unlike size/XS it does not exclude regenerated lockfiles.
//   - reverts
//   - `skip-issue-check` label (maintainer override -- deliberately the only
//     unconditional opt-out, and it needs write access. A self-service escape
//     hatch would make the rule optional for exactly the PRs it targets.)
//   - maintainers, by authorAssociation OR the .github/MAINTAINER file. Both are
//     needed: a maintainer whose org membership is private reads as CONTRIBUTOR,
//     and a maintainer may hold write access without being listed in the file.

const MS_PER_HOUR = 60 * 60 * 1000;
const MS_PER_DAY = 24 * MS_PER_HOUR;
const HOURS_TO_SCAN = 24;
// Days a PR may sit labeled `needs-issue` before it is closed. Matches the
// `waiting-on-author` window so contributors have one number to learn, and the
// same `/reopen` escape hatch applies.
const CLOSE_AFTER_DAYS = 7;
// The rule applies going forward only. PRs opened before this date are the
// backlog's problem, cleared by hand, and must never be flagged -- so the floor
// is a constant here rather than something a wider scan window could reach past.
const EFFECTIVE_FROM = "2026-08-05T00:00:00Z";
// Stamped on the bot's own comment for provenance (same approach as
// reopen-notice.js). Dedupe is the label's job, not this marker's.
const MARKER = "<!-- pr-issue-link -->";
// Dedupe, search key, and close clock in one. See the header.
const NEEDS_ISSUE_LABEL = "needs-issue";
// The only unconditional opt-out, and it needs write access. Removing
// `needs-issue` is not one: that label is recomputed every run.
const OVERRIDE_LABEL = "skip-issue-check";
// Same threshold pr-size.js uses for size/XS.
const TRIVIAL_LINES = 9;

const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

// Change types that describe work with no user-visible behaviour, and so no
// tracking issue. Must match the "Type of change" boxes in
// .github/pull_request_template.md.
const DECLARED_EXEMPT_TYPE = /- \[[xX]\]\s*(?:Refactor \/ chore|Docs|Test \/ CI)\b/;
// Types that always want an issue. Checked alongside an exempt type, these win:
// otherwise ticking `Test / CI` next to `Bug fix` is a free opt-out.
const DECLARED_TRACKED_TYPE = /- \[[xX]\]\s*(?:Bug fix|Feature|UI \/ frontend change)\b/;

// Non-closing references to an issue. GitHub only creates a *link* for the
// closing keywords, so these never reach closingIssuesReferences -- but they do
// say the work is tracked, which is what the rule is actually asking for. A PR
// that only partly addresses an issue should not have to claim it closes it.
// Deliberately excludes a bare `#123`, which is a cross-reference rather than a
// statement about this PR.
const TRACKING_REFERENCE =
  /\b(?:part of|related to|towards?|refs?|references?|see(?:\s+also)?)\b[:\s]*(?:https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/issues\/(\d+)|(?:[\w.-]+\/[\w.-]+)?#(\d+))/gi;

// Strips text that is being shown rather than asserted: fenced code blocks and
// blockquoted lines. Without this, a PR that quotes documentation containing
// "Part of #123" satisfies its own rule, which happened on the first live run.
function assertedText(body) {
  return (body ?? "")
    .replace(/```[\s\S]*?(?:```|$)/g, "")
    .replace(/~~~[\s\S]*?(?:~~~|$)/g, "")
    .split("\n")
    .filter((line) => !/^\s*>/.test(line))
    .join("\n");
}

// Issue numbers a body claims to be working towards, deduped and in order.
function trackingReferences(body) {
  const seen = [];
  for (const m of assertedText(body).matchAll(TRACKING_REFERENCE)) {
    const n = Number(m[1] ?? m[2]);
    if (n && !seen.includes(n)) seen.push(n);
  }
  return seen;
}

// Resolve one reference: is it an OPEN, non-draft issue in this repo?
//
// Shared so the nudge and the ready-for-review gate cannot drift on what counts.
//   - a pull request is not a tracking record
//   - a closed issue is not tracked work
//   - a draft issue is not agreed work yet
// Returns false when the number cannot be resolved: unverifiable is not evidence.
async function resolvesToOpenIssue({ github, core, owner, repo, number }) {
  try {
    const { data } = await github.rest.issues.get({ owner, repo, issue_number: number });
    if (data.pull_request) return false;
    if (data.state !== "open") return false;
    if (data.draft) return false;
    return true;
  } catch (err) {
    core?.warning?.(`Could not resolve #${number}: ${err.message}`);
    return false;
  }
}

const QUERY = `
  query($cursor: String, $searchQuery: String!) {
    rateLimit { remaining resetAt }
    search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number
          title
          isDraft
          additions
          deletions
          authorAssociation
          author { login __typename }
          labels(first: 30) { nodes { name } }
          body
        }
      }
    }
  }
`;

// The same node shape as QUERY, for one named PR. `state` and `createdAt` are
// extra: an event can name a PR that has since closed, or one predating the
// effective date, and neither should be touched.
const ONE_PR_QUERY = `
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        number
        title
        state
        createdAt
        isDraft
        additions
        deletions
        authorAssociation
        author { login __typename }
        labels(first: 30) { nodes { name } }
        body
      }
    }
  }
`;

// Resolved per PR rather than in the batch search above: the search connection
// under-reports closingIssuesReferences, and a false "unlinked" verdict is the
// one mistake that reaches a contributor.
const LINK_QUERY = `
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        closingIssuesReferences(first: 1) { totalCount }
      }
    }
  }
`;

function isBot(pr) {
  const author = pr.author || {};
  return author.__typename === "Bot" || (author.login || "").endsWith("[bot]");
}

// Returns the reason this PR is exempt, or null when the rule applies.
// `maintainers` is the lowercased login set from .github/MAINTAINER.
// Order matters only for which reason gets reported.
function exemptReason(pr, maintainers = new Set()) {
  const body = pr.body ?? "";
  const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
  if (isBot(pr)) return "bot";
  if (pr.isDraft) return "draft";
  if (MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation)) return "maintainer";
  if (maintainers.has((pr.author?.login ?? "").toLowerCase())) return "maintainer";
  if (labels.includes(OVERRIDE_LABEL)) return `${OVERRIDE_LABEL} label`;
  if (DECLARED_EXEMPT_TYPE.test(body) && !DECLARED_TRACKED_TYPE.test(body)) {
    return "declared chore/docs/test";
  }
  if ((pr.additions ?? 0) + (pr.deletions ?? 0) <= TRIVIAL_LINES) return "trivial";
  if (/^\s*revert\b/i.test(pr.title ?? "")) return "revert";
  return null;
}

const message = (author) =>
  `@${author} Thanks for the PR! It doesn't reference an issue yet.

**We require an issue for every PR**, so the work can be prioritized before it's reviewed. Add one to the description:

- \`Closes #123\` if this PR finishes the issue. That links it, gives your PR the issue's priority, and closes the issue when this merges. You can also link it from the **Development** section of the sidebar.
- \`Part of #123\` if this is one step towards it. \`Related to\`, \`Towards\`, and \`Refs\` work the same way, and leave the issue open.

No issue exists for this yet? Open one first, then reference it. That's how we track what's worth doing, and it's usually quicker than it sounds. Note a reference has to point at an issue: naming another PR doesn't count.

The only exceptions are changes with no user-visible behaviour: pure **Refactor / chore**, **Docs**, or **Test / CI** work. If that's genuinely what this is, check that box under *Type of change*. Anything that fixes a bug, adds a feature, or changes the UI needs an issue, even when it also touches docs or tests.

See [CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md#every-pr-needs-an-issue) for the full policy.

This PR is now labeled \`${NEEDS_ISSUE_LABEL}\`. **If no issue is referenced within ${CLOSE_AFTER_DAYS} days, it will be closed automatically** to keep the review queue readable. That is not a judgement on the change, and it is reversible: comment \`/reopen\` and the PR comes back. Referencing an issue clears the label, and the countdown with it.`;

const closeMessage = (labeledAt) =>
  `Closing this PR because it has been labeled \`${NEEDS_ISSUE_LABEL}\` for ${CLOSE_AFTER_DAYS} days without an issue reference.

The label was applied on ${labeledAt}. This isn't a judgement on the merit of the PR -- it's how we keep the review queue readable.

To continue: add \`Closes #123\` or \`Part of #123\` to the description, then comment \`/reopen\` and this PR comes back, as long as its source branch still exists. If the branch is gone, push it again and open a fresh PR referencing this one.

See [CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md#every-pr-needs-an-issue) for the full policy.`;

module.exports = async ({ context, github, core }) => {
  const { owner, repo } = context.repo;
  // Default to a dry run: enforcement is opt-in via the workflow env.
  const enforce = process.env.ENFORCE === "true";
  // Closing is gated separately from commenting, so the close half can ship in a
  // dry run (verdicts in the step summary, nothing touched) while the nudge stays
  // live. Implies ENFORCE: nothing may be closed over a label a dry run only
  // pretended to apply.
  const closeEnforce = enforce && process.env.CLOSE_ENFORCE === "true";
  // Unset means unlimited; an explicit LIMIT=0 means flag nothing. A malformed
  // value flags nothing rather than everything -- this bounds how many
  // contributors one run may comment on, so the safe default is the low one.
  const rawLimit = process.env.LIMIT;
  let limit = Infinity;
  if (rawLimit !== undefined && rawLimit !== "") {
    limit = Number(rawLimit);
    if (!Number.isFinite(limit)) {
      core.warning(`LIMIT=${rawLimit} is not a number; flagging nothing this run.`);
      limit = 0;
    }
  }

  try {
    // Load maintainers from the API, not the checked-out tree, so a PR can't
    // self-grant by editing the file (same approach as demo-check.js).
    const maintainers = new Set();
    try {
      const resp = await github.rest.repos.getContent({
        owner,
        repo,
        path: ".github/MAINTAINER",
        ref: context.payload.repository?.default_branch ?? "main",
      });
      Buffer.from(resp.data.content, "base64")
        .toString("utf8")
        .split("\n")
        .map((l) => l.replace(/#.*$/, "").trim().toLowerCase())
        .filter(Boolean)
        .forEach((m) => maintainers.add(m));
    } catch (err) {
      core.warning(`Could not load .github/MAINTAINER: ${err.message}`);
    }

    // Ensure the label exists before applying it, the same way demo-check.js does
    // for needs-demo. Only when enforcing: a dry run creates nothing.
    if (enforce) {
      try {
        await github.rest.issues.createLabel({
          owner,
          repo,
          name: NEEDS_ISSUE_LABEL,
          color: "d93f0b",
          description: `PR references no issue; closed after ${CLOSE_AFTER_DAYS} days`,
        });
      } catch (err) {
        // 422 = already exists; anything else is unexpected.
        if (err.status !== 422) {
          core.warning(`Could not create label '${NEEDS_ISSUE_LABEL}': ${err.message}`);
        }
      }
    }

    // Widens the sweep's window for a one-off manual run, so PRs older than a day
    // can be brought into the rule without hand-labeling them (which would skip
    // the nudge and start a clock nobody was warned about). EFFECTIVE_FROM still
    // floors it, so this can never reach the pre-rule backlog.
    let scanHours = HOURS_TO_SCAN;
    const rawScanHours = process.env.SCAN_HOURS;
    if (rawScanHours !== undefined && rawScanHours !== "") {
      const parsed = Number(rawScanHours);
      if (Number.isFinite(parsed) && parsed > 0) {
        scanHours = parsed;
      } else {
        core.warning(`SCAN_HOURS=${rawScanHours} is not a positive number; using ${HOURS_TO_SCAN}.`);
      }
    }

    // One PR when an event names it, the whole window on the cron sweep. Only the
    // fetch differs: every decision below runs identically either way, so the
    // instant path and the sweep can never reach different verdicts.
    const allPRs = [];
    const single = Number(process.env.PR_NUMBER) || null;
    if (single) {
      const resp = await github.graphql(ONE_PR_QUERY, { owner, repo, number: single });
      const pr = resp.repository.pullRequest;
      // The effective date still applies: an event on an older PR is not a licence
      // to reach into the backlog.
      if (!pr) {
        console.log(`#${single} not found; nothing to do.`);
      } else if (new Date(pr.createdAt) < new Date(EFFECTIVE_FROM)) {
        console.log(`#${single} predates ${EFFECTIVE_FROM}; skipping.`);
      } else if (pr.state !== "OPEN") {
        console.log(`#${single} is ${pr.state}; skipping.`);
      } else {
        allPRs.push(pr);
      }
      console.log(`Checking #${single} (enforce=${enforce})`);
    } else {
      const windowStart = new Date(Date.now() - scanHours * MS_PER_HOUR);
      // Never look further back than the effective date, whichever is later.
      const cutoff = new Date(
        Math.max(windowStart.getTime(), new Date(EFFECTIVE_FROM).getTime())
      );
      const stamp = (d) => d.toISOString().replace(/\.\d{3}Z$/, "Z");
      const search = async (searchQuery) => {
        console.log(`Scanning PRs: ${searchQuery} (enforce=${enforce})`);
        const found = [];
        let cursor = null;
        let hasNextPage = true;
        while (hasNextPage) {
          const response = await github.graphql(QUERY, { cursor, searchQuery });
          const { remaining, resetAt } = response.rateLimit;
          console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
          const { nodes, pageInfo } = response.search;
          hasNextPage = pageInfo.hasNextPage;
          cursor = pageInfo.endCursor;
          found.push(...nodes);
        }
        return found;
      };
      // Two passes: PRs new enough to flag, then every PR already flagged. The
      // second is the only route back to a PR older than the window, which is what
      // the close clock needs. The effective date still floors both.
      const seen = new Set();
      for (const searchQuery of [
        `repo:${owner}/${repo} is:pr is:open created:>${stamp(cutoff)}`,
        `repo:${owner}/${repo} is:pr is:open label:${NEEDS_ISSUE_LABEL} created:>${stamp(new Date(EFFECTIVE_FROM))}`,
      ]) {
        for (const pr of await search(searchQuery)) {
          if (seen.has(pr.number)) continue;
          seen.add(pr.number);
          allPRs.push(pr);
        }
      }
      console.log(`Found ${allPRs.length} open PRs to check`);
    }

    const hasNeedsIssue = (pr) =>
      (pr.labels?.nodes ?? []).some((l) => l.name === NEEDS_ISSUE_LABEL);

    // The label is derived state, so drop it the moment a PR stops being flaggable.
    // Returns the reason string, annotated with what it did, for the summary.
    const clearLabel = async (pr, why) => {
      if (!hasNeedsIssue(pr)) return why;
      if (!enforce) return `${why}; would clear ${NEEDS_ISSUE_LABEL}`;
      try {
        await github.rest.issues.removeLabel({
          owner,
          repo,
          issue_number: pr.number,
          name: NEEDS_ISSUE_LABEL,
        });
        return `${why}; cleared ${NEEDS_ISSUE_LABEL}`;
      } catch (err) {
        core.warning(`Could not clear ${NEEDS_ISSUE_LABEL} from #${pr.number}: ${err.message}`);
        return why;
      }
    };

    // When the label last went on, which is when the clock started. Null when no
    // such event exists: unverifiable is not grounds to close.
    const labeledAt = async (number) => {
      const events = await github.paginate(github.rest.issues.listEvents, {
        owner,
        repo,
        issue_number: number,
        per_page: 100,
      });
      const stamps = events
        .filter((e) => e.event === "labeled" && e.label?.name === NEEDS_ISSUE_LABEL)
        .map((e) => e.created_at)
        .sort();
      return stamps.length ? stamps[stamps.length - 1] : null;
    };

    const verdicts = [];
    let flagged = 0;
    let closed = 0;

    for (const pr of allPRs) {
      const exempt = exemptReason(pr, maintainers);
      if (exempt) {
        verdicts.push({ pr: pr.number, verdict: "exempt", reason: await clearLabel(pr, exempt) });
        continue;
      }

      // Authoritative link check: covers closing keywords, cross-repo refs,
      // full issue URLs, and issues linked from the sidebar (which a body
      // regex cannot see and which fires no webhook).
      let linkCount;
      try {
        const resp = await github.graphql(LINK_QUERY, { owner, repo, number: pr.number });
        linkCount = resp.repository.pullRequest.closingIssuesReferences.totalCount;
      } catch (err) {
        // Fail closed: an unverifiable PR is left alone rather than flagged.
        core.warning(`Could not resolve links for #${pr.number}: ${err.message}`);
        verdicts.push({ pr: pr.number, verdict: "skip", reason: "link lookup failed" });
        continue;
      }
      if (linkCount > 0) {
        const reason = await clearLabel(pr, `${linkCount} linked`);
        verdicts.push({ pr: pr.number, verdict: "ok", reason });
        continue;
      }

      // No closing link, but the body may still name the issue it works towards.
      // Each candidate is resolved: "Refs #4147" often points at another PR, and a
      // closed or draft issue is not tracked work.
      let tracked = null;
      for (const candidate of trackingReferences(pr.body)) {
        if (await resolvesToOpenIssue({ github, core, owner, repo, number: candidate })) {
          tracked = candidate;
          break;
        }
      }
      if (tracked) {
        const reason = await clearLabel(pr, `references #${tracked}`);
        verdicts.push({ pr: pr.number, verdict: "ok", reason });
        continue;
      }

      const author = pr.author?.login ?? "contributor";

      // Already flagged. Only these PRs pay for the timeline lookup, and the label
      // is what dedupes the nudge, so an unflagged PR costs nothing extra.
      if (hasNeedsIssue(pr)) {
        const since = await labeledAt(pr.number);
        if (!since) {
          core.warning(
            `#${pr.number} has ${NEEDS_ISSUE_LABEL} but no labeled event; not closing.`
          );
          verdicts.push({ pr: pr.number, verdict: "skip", reason: "no label timestamp" });
          continue;
        }
        const days = Math.floor((Date.now() - new Date(since).getTime()) / MS_PER_DAY);
        if (days < CLOSE_AFTER_DAYS) {
          verdicts.push({
            pr: pr.number,
            verdict: "waiting",
            reason: `day ${days} of ${CLOSE_AFTER_DAYS}`,
          });
          continue;
        }
        const overdue = `labeled ${days}d ago`;
        if (!closeEnforce) {
          verdicts.push({ pr: pr.number, verdict: "WOULD CLOSE", reason: overdue });
          continue;
        }
        // LIMIT bounds closures the same way it bounds nudges: a mistake in the
        // predicate must not reach the whole queue in one sweep.
        if (closed >= limit) {
          verdicts.push({ pr: pr.number, verdict: "deferred", reason: "close limit reached" });
          continue;
        }
        verdicts.push({ pr: pr.number, verdict: "CLOSED", reason: overdue });
        closed++;
        // Comment first, so the explanation is already there when it closes.
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: pr.number,
          body: closeMessage(since.slice(0, 10)),
        });
        await github.rest.issues.update({
          owner,
          repo,
          issue_number: pr.number,
          state: "closed",
        });
        continue;
      }

      // A dry run enumerates every verdict -- that's its whole point, so LIMIT
      // (which bounds how many contributors one enforcing run may touch) must
      // not truncate the list an operator reviews before enabling.
      if (!enforce) {
        verdicts.push({ pr: pr.number, verdict: "FLAG", reason: `@${author}` });
        continue;
      }

      if (flagged >= limit) {
        verdicts.push({ pr: pr.number, verdict: "deferred", reason: "run limit reached" });
        continue;
      }

      verdicts.push({ pr: pr.number, verdict: "FLAG", reason: `@${author}` });
      flagged++;
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pr.number,
        body: `${MARKER}\n${message(author)}`,
      });
      // The label is the clock, so it has to go on with the comment. Applied after
      // it, so a failure here leaves an unflagged PR that the next run retries
      // rather than a silent clock with no warning attached.
      try {
        await github.rest.issues.addLabels({
          owner,
          repo,
          issue_number: pr.number,
          labels: [NEEDS_ISSUE_LABEL],
        });
      } catch (err) {
        core.warning(`Could not label #${pr.number} ${NEEDS_ISSUE_LABEL}: ${err.message}`);
      }
    }

    const counts = verdicts.reduce((acc, v) => {
      acc[v.verdict] = (acc[v.verdict] || 0) + 1;
      return acc;
    }, {});
    const summary = Object.entries(counts).map(([k, n]) => `${k}=${n}`).join(" ");
    console.log(`Done (enforce=${enforce}). ${summary}`);

    // The full verdict list, so a dry run can be reviewed before enforcing.
    if (core.summary) {
      core.summary
        .addHeading(`Issue-link check ${enforce ? "(enforcing)" : "(dry run, nothing changed)"}`, 3)
        .addRaw(`\n${summary}\n\n`)
        .addTable([
          [
            { data: "PR", header: true },
            { data: "Verdict", header: true },
            { data: "Reason", header: true },
          ],
          ...verdicts.map((v) => [`#${v.pr}`, v.verdict, v.reason]),
        ]);
      await core.summary.write();
    }
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log("Rate limit hit. Exiting gracefully.");
      return;
    }
    throw error;
  }
};

// Exported for the offline unit test.
module.exports.exemptReason = exemptReason;
module.exports.trackingReferences = trackingReferences;
module.exports.assertedText = assertedText;
module.exports.resolvesToOpenIssue = resolvesToOpenIssue;
module.exports.MARKER = MARKER;
module.exports.EFFECTIVE_FROM = EFFECTIVE_FROM;
module.exports.NEEDS_ISSUE_LABEL = NEEDS_ISSUE_LABEL;
module.exports.CLOSE_AFTER_DAYS = CLOSE_AFTER_DAYS;
