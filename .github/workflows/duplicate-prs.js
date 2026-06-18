// Close duplicate community PRs that reference (close) the same issue.
// Only considers open PRs created in the last 14 days. For each issue with
// more than one such PR, the oldest PR is kept and the newer ones are closed,
// labeled `duplicate`, and commented on. Maintainer PRs are included in
// detection (so a maintainer's PR can be the kept "keeper") but are never
// auto-closed: a maintainer duplicate instead gets a softer heads-up comment
// and the `duplicate` label (no close). Originally ported from mlflow/mlflow's
// .github/workflows/duplicate-prs.js, with the maintainer skip narrowed from
// detection to closing only.

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DAYS_TO_CONSIDER = 14;
const DUPLICATE_LABEL = "duplicate";

const duplicateMessage = (author, issueNumber, keeperPR) =>
  `@${author} This PR appears to reference the same issue (#${issueNumber}) as #${keeperPR} (opened earlier). Closing as a duplicate.`;

// Maintainer duplicates are flagged but not auto-closed -- a softer, no-action
// heads-up so the maintainer can decide what to do.
const maintainerDuplicateMessage = (author, issueNumber, keeperPR) =>
  `@${author} This PR may be a duplicate -- it references the same issue (#${issueNumber}) as #${keeperPR} (opened earlier). It won't be auto-closed since it's a maintainer PR; please close it manually if it is indeed a duplicate.`;

// GraphQL query to fetch open PRs created in the search window.
const QUERY = `
  query($cursor: String, $searchQuery: String!) {
    rateLimit { remaining resetAt }
    search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        ... on PullRequest {
          number
          createdAt
          url
          author { login }
          authorAssociation
          labels(first: 20) { nodes { name } }
          closingIssuesReferences(first: 10) {
            nodes {
              number
            }
          }
        }
      }
    }
  }
`;

const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

// Maintainer PRs participate in detection (so they can be the kept "keeper"
// that makes a community duplicate closeable) but are never themselves closed.
const isMaintainerPR = (pr) => MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation);

// Whether a PR should be considered at all when grouping by issue. Already
// labeled-duplicate PRs are skipped (already handled); everything else --
// community and maintainer alike -- is considered.
const shouldConsiderPR = (pr) => {
  const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
  return !labels.includes(DUPLICATE_LABEL);
};

// Whether a duplicate PR is eligible to be auto-closed: only community PRs.
const canClosePR = (pr) => !isMaintainerPR(pr);

const getIssueReferences = (pr) => {
  const references = pr.closingIssuesReferences?.nodes || [];
  return references.map((node) => node.number);
};

module.exports = async ({ context, github }) => {
  const { owner, repo } = context.repo;

  try {
    // Calculate the start of the search window.
    const cutoff = new Date(Date.now() - DAYS_TO_CONSIDER * MS_PER_DAY);
    const dateString = cutoff.toISOString().slice(0, 10);
    const searchQuery = `repo:${owner}/${repo} is:pr is:open created:>${dateString}`;

    console.log(`Searching for PRs: ${searchQuery}`);

    let cursor = null;
    let hasNextPage = true;
    const allPRs = [];

    // Fetch all open PRs from the search window.
    while (hasNextPage) {
      const response = await github.graphql(QUERY, { cursor, searchQuery });
      const { remaining, resetAt } = response.rateLimit;
      console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);

      const { nodes, pageInfo } = response.search;
      hasNextPage = pageInfo.hasNextPage;
      cursor = pageInfo.endCursor;

      allPRs.push(...nodes);
    }

    console.log(`Found ${allPRs.length} open PRs from the last ${DAYS_TO_CONSIDER} days`);

    // Consider every open PR (community and maintainer) that isn't already
    // labeled a duplicate -- a maintainer PR can still be the kept "keeper".
    const consideredPRs = allPRs.filter(shouldConsiderPR);
    console.log(`${consideredPRs.length} PRs are eligible for grouping`);

    // Group PRs by the single issue they reference.
    // Skip PRs that reference multiple issues (ambiguous intent).
    const prsByIssue = new Map();

    for (const pr of consideredPRs) {
      const issueRefs = getIssueReferences(pr);

      if (issueRefs.length === 0) {
        // PR doesn't reference any issue, skip it.
        continue;
      }

      if (issueRefs.length > 1) {
        // PR references multiple issues, skip it (ambiguous).
        console.log(
          `Skipping PR #${pr.number}: references multiple issues (${issueRefs.join(", ")})`
        );
        continue;
      }

      // PR references exactly one issue.
      const issueNumber = issueRefs[0];
      if (!prsByIssue.has(issueNumber)) {
        prsByIssue.set(issueNumber, []);
      }
      prsByIssue.get(issueNumber).push(pr);
    }

    console.log(`Found ${prsByIssue.size} issues with associated PRs`);

    // Process each issue that has multiple PRs.
    let closedCount = 0;
    let flaggedCount = 0;
    for (const [issueNumber, prs] of prsByIssue.entries()) {
      if (prs.length <= 1) {
        // Only one PR for this issue, no duplicates.
        continue;
      }

      console.log(`Issue #${issueNumber} has ${prs.length} PRs`);

      // Sort PRs by creation date (oldest first). Break ties on PR number
      // (lower = opened earlier) so "keep the oldest" is deterministic when two
      // PRs share a createdAt timestamp.
      prs.sort(
        (a, b) => new Date(a.createdAt) - new Date(b.createdAt) || a.number - b.number
      );

      // Keep the oldest PR, close the rest as duplicates.
      const [keeper, ...duplicates] = prs;
      console.log(`  Keeping PR #${keeper.number} (oldest, created ${keeper.createdAt})`);

      for (const pr of duplicates) {
        // pr.author is null for deleted/ghost accounts; fall back gracefully.
        const author = pr.author?.login ?? "contributor";

        // Maintainer duplicates are flagged but never auto-closed: post a
        // heads-up comment, then label so the next run doesn't re-flag them
        // (the label excludes the PR from grouping via shouldConsiderPR).
        // Comment before labeling so a label failure re-posts rather than
        // silently swallowing the heads-up.
        if (!canClosePR(pr)) {
          console.log(`  Flagging PR #${pr.number} as a possible duplicate (maintainer PR -- not auto-closed)`);

          await github.rest.issues.createComment({
            owner,
            repo,
            issue_number: pr.number,
            body: maintainerDuplicateMessage(author, issueNumber, keeper.number),
          });

          await github.rest.issues.addLabels({
            owner,
            repo,
            issue_number: pr.number,
            labels: [DUPLICATE_LABEL],
          });

          flaggedCount++;
          continue;
        }

        console.log(`  Closing PR #${pr.number} as duplicate (created ${pr.createdAt})`);

        // Close first so a failure here leaves the PR open and unlabeled,
        // letting the next run retry. If we labeled first and then failed
        // to close, shouldConsiderPR would skip the PR forever.
        await github.rest.pulls.update({
          owner,
          repo,
          pull_number: pr.number,
          state: "closed",
        });

        await github.rest.issues.addLabels({
          owner,
          repo,
          issue_number: pr.number,
          labels: [DUPLICATE_LABEL],
        });

        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: pr.number,
          body: duplicateMessage(author, issueNumber, keeper.number),
        });

        closedCount++;
      }
    }

    console.log(`Closed ${closedCount} duplicate PRs; flagged ${flaggedCount} maintainer PRs.`);
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log(`Rate limit hit. Exiting gracefully.`);
      return;
    }
    throw error;
  }
};
