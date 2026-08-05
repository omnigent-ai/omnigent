// Scan PRs opened in the last 24 hours and flag any that don't link an issue.
// Runs hourly from the demo-check sweep; the 24-hour window ensures every new PR
// is checked even if it was opened just before a cron tick. A flagged PR gets one
// comment and nothing else: no label (it would only add noise to the queue
// maintainers filter) and no close.
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
const HOURS_TO_SCAN = 24;
// The rule applies going forward only. PRs opened before this date are the
// backlog's problem, cleared by hand, and must never be flagged -- so the floor
// is a constant here rather than something a wider scan window could reach past.
const EFFECTIVE_FROM = "2026-08-05T00:00:00Z";
// Dedupe on a hidden marker in the bot's own comment rather than a label: the
// nudge is a one-shot message, and a label on top of it would add queue noise
// maintainers have to filter past (same approach as reopen-notice.js).
const MARKER = "<!-- pr-issue-link -->";
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

_No action is taken beyond this comment._`;

module.exports = async ({ context, github, core }) => {
  const { owner, repo } = context.repo;
  // Default to a dry run: enforcement is opt-in via the workflow env.
  const enforce = process.env.ENFORCE === "true";
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

    const windowStart = new Date(Date.now() - HOURS_TO_SCAN * MS_PER_HOUR);
    // Never look further back than the effective date, whichever is later.
    const cutoff = new Date(
      Math.max(windowStart.getTime(), new Date(EFFECTIVE_FROM).getTime())
    );
    const cutoffString = cutoff.toISOString().replace(/\.\d{3}Z$/, "Z");
    const searchQuery = `repo:${owner}/${repo} is:pr is:open created:>${cutoffString}`;
    console.log(`Scanning PRs: ${searchQuery} (enforce=${enforce})`);

    let cursor = null;
    let hasNextPage = true;
    const allPRs = [];
    while (hasNextPage) {
      const response = await github.graphql(QUERY, { cursor, searchQuery });
      const { remaining, resetAt } = response.rateLimit;
      console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
      const { nodes, pageInfo } = response.search;
      hasNextPage = pageInfo.hasNextPage;
      cursor = pageInfo.endCursor;
      allPRs.push(...nodes);
    }
    console.log(`Found ${allPRs.length} open PRs from the last ${HOURS_TO_SCAN} hours`);

    const verdicts = [];
    let flagged = 0;

    for (const pr of allPRs) {
      const exempt = exemptReason(pr, maintainers);
      if (exempt) {
        verdicts.push({ pr: pr.number, verdict: "exempt", reason: exempt });
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
        verdicts.push({ pr: pr.number, verdict: "ok", reason: `${linkCount} linked` });
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
        verdicts.push({ pr: pr.number, verdict: "ok", reason: `references #${tracked}` });
        continue;
      }

      const author = pr.author?.login ?? "contributor";

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

      // Only PRs about to be nudged pay for the comment lookup. Checked here
      // rather than up front so the dry run doesn't spend a request per PR.
      const comments = await github.paginate(github.rest.issues.listComments, {
        owner,
        repo,
        issue_number: pr.number,
        per_page: 100,
      });
      if (comments.some((c) => c.body?.includes(MARKER))) {
        verdicts.push({ pr: pr.number, verdict: "skip", reason: "already nudged" });
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
