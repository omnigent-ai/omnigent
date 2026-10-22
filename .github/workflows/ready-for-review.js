// Put a fresh PR into `waiting-for-review` once it clears the minimum bar, so
// maintainers have a queue of PRs that are actually reviewable rather than the
// whole open list.
//
// Until now `waiting-for-review` had exactly one entrance: the handoff that fires
// when an author replies to feedback. A PR nobody had touched yet sat in neither
// state, which is why almost every open PR carries no review-state label.
//
// The bar today is deliberately just "references an issue". It is meant to rise:
// CI green, demo present, Polly clean. Each is a predicate added to `meetsBar`,
// and the rest of this file stays the same.
//
// Never applied when:
//   - the author is a maintainer or a bot. The label exists to route incoming
//     contributions; maintainers land their own work and half the in-window PRs
//     are theirs, so labelling them halves the signal. It matches the nudge, which
//     exempts maintainers for the same reason.
//   - the PR is closed or merged. `is:open` in the search lags, so one that closed
//     in the last few minutes still comes back and must not be labelled.
//   - the PR is a draft (the author is telling us it is not ready)
//   - `waiting-on-author` is set (the ball is in the author's court; applying
//     both would break the mutual exclusion the two labels rely on)
//   - the label is already there (idempotent), or a human removed it before
//
// Forward-only, sharing pr-issue-link.js's effective date: labelling 478 backlog
// PRs in one sweep would bury the signal it exists to create.

const issueLink = require("./pr-issue-link.js");

const MS_PER_HOUR = 60 * 60 * 1000;
const HOURS_TO_SCAN = 24;
const REVIEW_LABEL = "waiting-for-review";
const WAITING_LABEL = "waiting-on-author";
const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

const QUERY = `
  query($cursor: String, $searchQuery: String!) {
    rateLimit { remaining resetAt }
    search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number
          state
          isDraft
          authorAssociation
          author { login __typename }
          labels(first: 30) { nodes { name } }
          body
          timelineItems(last: 50, itemTypes: [UNLABELED_EVENT]) {
            nodes {
              ... on UnlabeledEvent {
                label { name }
                actor { login }
              }
            }
          }
        }
      }
    }
  }
`;

// The same node shape as QUERY, for one named PR, plus createdAt for the
// effective-date floor.
const ONE_PR_QUERY = `
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        number
        state
        createdAt
        isDraft
        authorAssociation
        author { login __typename }
        labels(first: 30) { nodes { name } }
        body
        timelineItems(last: 50, itemTypes: [UNLABELED_EVENT]) {
          nodes {
            ... on UnlabeledEvent {
              label { name }
              actor { login }
            }
          }
        }
      }
    }
  }
`;

const LINK_QUERY = `
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        closingIssuesReferences(first: 1) { totalCount }
      }
    }
  }
`;

// True when a HUMAN removed this label before. A maintainer who takes it off is
// saying "not ready", and a sweep that reapplies it every hour would be arguing
// with them.
//
// The actor check is the whole point: waiting_on_author.py removes this label
// itself whenever `waiting-on-author` goes on, since the two are mutually
// exclusive. Counting that bot removal would permanently disqualify any PR that
// has ever been through a review round trip, which is most of them.
function removedByHuman(pr) {
  const events = pr.timelineItems?.nodes ?? [];
  return events.some(
    (e) =>
      e?.label?.name === REVIEW_LABEL &&
      e?.actor?.login &&
      !e.actor.login.endsWith("[bot]")
  );
}

// Does this PR reference an issue? Reuses the same resolution as the nudge, so
// the gate and the nudge can never disagree about what counts.
async function referencesIssue({ github, core, owner, repo, pr }) {
  try {
    const resp = await github.graphql(LINK_QUERY, { owner, repo, number: pr.number });
    if (resp.repository.pullRequest.closingIssuesReferences.totalCount > 0) return true;
  } catch (err) {
    // Unverifiable: say no rather than labelling on a guess.
    core.warning(`Could not resolve links for #${pr.number}: ${err.message}`);
    return false;
  }
  for (const candidate of issueLink.trackingReferences(pr.body)) {
    if (await issueLink.resolvesToOpenIssue({ github, core, owner, repo, number: candidate })) {
      return true;
    }
  }
  return false;
}

// Returns null when the PR is ready, or the reason it is not.
async function belowBar(ctx) {
  if (!(await referencesIssue(ctx))) return "no issue referenced";
  return null;
}

// True when the PR is the project's own work rather than an incoming contribution.
// Checked on both signals, like the nudge: a maintainer whose org membership is
// private reads as CONTRIBUTOR, and one with write access may be unlisted.
function isOwnWork(pr, maintainers) {
  const login = pr.author?.login ?? "";
  if (pr.author?.__typename === "Bot" || login.endsWith("[bot]")) return "bot";
  if (MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation)) return "maintainer";
  if (maintainers.has(login.toLowerCase())) return "maintainer";
  return null;
}

module.exports = async ({ context, github, core }) => {
  const { owner, repo } = context.repo;
  const enforce = process.env.ENFORCE === "true";

  try {
    // One PR when an event names it, the whole window on the cron sweep. Only the
    // fetch differs, so both routes reach identical verdicts.
    const allPRs = [];
    const single = Number(process.env.PR_NUMBER) || null;
    if (single) {
      const resp = await github.graphql(ONE_PR_QUERY, { owner, repo, number: single });
      const pr = resp.repository.pullRequest;
      if (!pr) {
        console.log(`#${single} not found; nothing to do.`);
      } else if (new Date(pr.createdAt) < new Date(issueLink.EFFECTIVE_FROM)) {
        console.log(`#${single} predates ${issueLink.EFFECTIVE_FROM}; skipping.`);
      } else {
        allPRs.push(pr);
      }
      console.log(`Checking #${single} (enforce=${enforce})`);
    } else {
      const windowStart = new Date(Date.now() - HOURS_TO_SCAN * MS_PER_HOUR);
      const cutoff = new Date(
        Math.max(windowStart.getTime(), new Date(issueLink.EFFECTIVE_FROM).getTime())
      );
      const cutoffString = cutoff.toISOString().replace(/\.\d{3}Z$/, "Z");
      const searchQuery = `repo:${owner}/${repo} is:pr is:open created:>${cutoffString}`;
      console.log(`Scanning PRs: ${searchQuery} (enforce=${enforce})`);

      let cursor = null;
      let hasNextPage = true;
      while (hasNextPage) {
        const response = await github.graphql(QUERY, { cursor, searchQuery });
        const { remaining, resetAt } = response.rateLimit;
        console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
        const { nodes, pageInfo } = response.search;
        hasNextPage = pageInfo.hasNextPage;
        cursor = pageInfo.endCursor;
        allPRs.push(...nodes);
      }
      console.log(`Found ${allPRs.length} open PRs in the window`);
    }

    // Read from the API, not the checked-out tree, so a PR cannot self-grant by
    // editing the file (same approach as the nudge).
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

    const verdicts = [];
    for (const pr of allPRs) {
      const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
      let skip = isOwnWork(pr, maintainers);
      if (skip) {
        // own work: reported as-is
      }
      // `is:open` in the search is index-backed and lags, so a PR closed or merged
      // in the last few minutes still comes back. Check the state we were handed.
      else if (pr.state !== "OPEN") skip = pr.state.toLowerCase();
      else if (pr.isDraft) skip = "draft";
      else if (labels.includes(REVIEW_LABEL)) skip = "already labelled";
      else if (labels.includes(WAITING_LABEL)) skip = "waiting on author";
      else if (removedByHuman(pr)) skip = "label was removed by hand";
      if (skip) {
        verdicts.push({ pr: pr.number, verdict: "skip", reason: skip });
        continue;
      }

      const reason = await belowBar({ github, core, owner, repo, pr });
      if (reason) {
        verdicts.push({ pr: pr.number, verdict: "below bar", reason });
        continue;
      }

      verdicts.push({ pr: pr.number, verdict: "READY", reason: "meets the bar" });
      if (!enforce) continue;
      // Per-PR, so one failed write does not abandon the rest of the sweep. The
      // label is idempotent and the sweep is hourly, so a miss self-heals.
      try {
        await github.rest.issues.addLabels({
          owner,
          repo,
          issue_number: pr.number,
          labels: [REVIEW_LABEL],
        });
        console.log(`Added ${REVIEW_LABEL} to #${pr.number}`);
      } catch (err) {
        if (err.status === 429 || err.message?.includes("rate limit")) throw err;
        core.warning(`Could not label #${pr.number}: ${err.message}`);
      }
    }

    const counts = verdicts.reduce((acc, v) => {
      acc[v.verdict] = (acc[v.verdict] || 0) + 1;
      return acc;
    }, {});
    const summary = Object.entries(counts)
      .map(([k, n]) => `${k}=${n}`)
      .join(" ");
    console.log(`Done (enforce=${enforce}). ${summary}`);

    if (core.summary) {
      core.summary
        .addHeading(
          `Ready-for-review gate ${enforce ? "(enforcing)" : "(dry run, nothing changed)"}`,
          3
        )
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

module.exports.removedByHuman = removedByHuman;
module.exports.REVIEW_LABEL = REVIEW_LABEL;
