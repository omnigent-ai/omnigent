// Scan open contributor PRs and comment when a UI / frontend change is checked
// but no demo (screenshot / video) is provided. Runs on a schedule so it
// catches PRs that were opened without a demo and never updated. Maintainer
// PRs are skipped. Already-flagged PRs (labeled `needs-demo`) are skipped on
// subsequent runs to avoid duplicate comments.

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DAYS_TO_SCAN = 14;
const NEEDS_DEMO_LABEL = "needs-demo";
const COMMENT_MARKER = "<!-- demo-check -->";

const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

// Sentinel text that the PR template seeds into the Demo section. A Demo
// section whose only non-whitespace content is one of these (possibly with the
// surrounding HTML comment stripped) is treated as "not provided".
const PLACEHOLDER_PATTERNS = [
  /^n\/a$/i,
  /^none$/i,
  /^-$/,
  /^tbd$/i,
  /^todo$/i,
];

const QUERY = `
  query($cursor: String, $searchQuery: String!) {
    rateLimit { remaining resetAt }
    search(query: $searchQuery, type: ISSUE, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        ... on PullRequest {
          number
          author { login }
          authorAssociation
          isDraft
          labels(first: 20) { nodes { name } }
          body
        }
      }
    }
  }
`;

// Returns true when the "UI / frontend change" checkbox is checked.
function hasUIChangeChecked(body) {
  return /- \[[xX]\] UI \/ frontend change/.test(body ?? "");
}

// Extracts the text content of the Demo section (between ## Demo and the next
// ## heading or end of string), strips HTML comments, and trims whitespace.
function extractDemoContent(body) {
  const text = body ?? "";
  // Find the start of the ## Demo heading (match exactly, no greedy \s*
  // consuming the content line).
  const startMatch = /^## Demo[ \t]*$/m.exec(text);
  if (!startMatch) return "";
  const afterHeading = text.slice(startMatch.index + startMatch[0].length);
  // Find the next ## heading to bound the section.
  const nextHeading = /^## /m.exec(afterHeading);
  const section = nextHeading
    ? afterHeading.slice(0, nextHeading.index)
    : afterHeading;
  return section
    .replace(/<!--[\s\S]*?-->/g, "")   // complete HTML comments
    .replace(/<!--[\s\S]*/g, "")        // unclosed comment remnants
    .trim();
}

// Returns true when the demo section has real content (not empty / placeholder).
function hasDemoContent(body) {
  const content = extractDemoContent(body);
  if (!content) return false;
  return !PLACEHOLDER_PATTERNS.some((re) => re.test(content));
}

const demoRequiredMessage = (author) =>
  `${COMMENT_MARKER}
@${author} This PR checks **UI / frontend change** but the **Demo** section is missing or only contains a placeholder.

UI changes require a screenshot or screen recording so reviewers can see the new behaviour without checking out the branch. Please update the **Demo** section with:

- A screenshot or screen recording of the change, or
- A link to a hosted video or GIF showing the new behaviour.

_Use \`N/A\` only for non-visual changes. If this PR does not actually modify the UI, uncheck the **UI / frontend change** box instead._`;

module.exports = async ({ context, github, core }) => {
  const { owner, repo } = context.repo;

  try {
    // Load maintainers from the API so a PR can't self-grant by editing the
    // file (same approach as maintainer-approval.yml).
    let maintainers = new Set();
    try {
      const resp = await github.rest.repos.getContent({
        owner,
        repo,
        path: ".github/MAINTAINER",
        ref: "main",
      });
      const decoded = Buffer.from(resp.data.content, "base64").toString("utf8");
      decoded
        .split("\n")
        .map((l) => l.replace(/#.*$/, "").trim().toLowerCase())
        .filter(Boolean)
        .forEach((m) => maintainers.add(m));
    } catch (err) {
      core.warning(`Could not load .github/MAINTAINER: ${err.message}`);
    }

    // Ensure the needs-demo label exists before we try to apply it.
    try {
      await github.rest.issues.createLabel({
        owner,
        repo,
        name: NEEDS_DEMO_LABEL,
        color: "e4e669",
        description: "PR needs a demo screenshot or recording",
      });
    } catch (err) {
      // 422 = already exists; anything else is unexpected.
      if (err.status !== 422) {
        core.warning(`Could not create label '${NEEDS_DEMO_LABEL}': ${err.message}`);
      }
    }

    const cutoff = new Date(Date.now() - DAYS_TO_SCAN * MS_PER_DAY);
    const dateString = cutoff.toISOString().slice(0, 10);
    const searchQuery = `repo:${owner}/${repo} is:pr is:open created:>${dateString}`;

    console.log(`Scanning PRs: ${searchQuery}`);

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

    console.log(`Found ${allPRs.length} open PRs from the last ${DAYS_TO_SCAN} days`);

    let flaggedCount = 0;
    let skippedCount = 0;

    for (const pr of allPRs) {
      // Skip bots, drafts, and maintainer-association PRs.
      if (pr.isDraft) {
        skippedCount++;
        continue;
      }
      if (MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation)) {
        skippedCount++;
        continue;
      }

      const author = pr.author?.login ?? "contributor";
      if (maintainers.has(author.toLowerCase())) {
        skippedCount++;
        continue;
      }

      // Skip PRs we've already flagged.
      const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
      if (labels.includes(NEEDS_DEMO_LABEL)) {
        skippedCount++;
        continue;
      }

      // Only care about PRs that checked the UI / frontend change box.
      if (!hasUIChangeChecked(pr.body)) {
        continue;
      }

      // Demo content is present — nothing to do.
      if (hasDemoContent(pr.body)) {
        continue;
      }

      console.log(`PR #${pr.number} (@${author}): UI change checked but no demo provided`);

      // Label before commenting so a comment failure leaves the PR labeled and
      // won't be re-commented on the next run.
      await github.rest.issues.addLabels({
        owner,
        repo,
        issue_number: pr.number,
        labels: [NEEDS_DEMO_LABEL],
      });

      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pr.number,
        body: demoRequiredMessage(author),
      });

      flaggedCount++;
    }

    console.log(
      `Done. Flagged ${flaggedCount} PR(s); skipped ${skippedCount} (drafts / maintainers / already labeled).`
    );
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log("Rate limit hit. Exiting gracefully.");
      return;
    }
    throw error;
  }
};
