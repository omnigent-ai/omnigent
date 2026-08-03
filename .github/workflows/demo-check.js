// Scan contributor PRs and comment when a Bug fix, Feature, or UI / frontend
// change is checked but no real demo (screenshot / video) is provided. Runs
// hourly over two searches, unioned and deduped: PRs opened in the last 24
// hours (so every new PR is seen even if opened just before a cron tick), plus
// every open PR already labeled `needs-demo` regardless of age. Drafts and
// maintainer PRs are skipped. While a PR carries `needs-demo` it is never
// commented again; an already-labeled PR is re-evaluated each run and the
// label cleared once its Demo section is satisfied (or the change type no
// longer needs one). A PR that later regresses can be flagged afresh.

const MS_PER_HOUR = 60 * 60 * 1000;
const HOURS_TO_SCAN = 24;
const NEEDS_DEMO_LABEL = "needs-demo";

const MAINTAINER_ASSOCIATIONS = ["MEMBER", "OWNER", "COLLABORATOR"];

// Patterns that match real demo media in the Demo section.
// A demo is considered present only when one of these is found.
const DEMO_MEDIA_PATTERNS = [
  /!\[.*?\]\(https?:\/\//,           // Markdown image with URL: ![alt](https://...)
  /<img\b[^>]+src=/i,                // HTML <img src="...">
  /https?:\/\/\S+\.(?:gif|mp4|mov|webm|mkv)/i,  // direct video/gif URL
  /https?:\/\/(?:www\.)?loom\.com\//i,           // Loom recording
  /https?:\/\/(?:www\.)?youtube\.com\/|https?:\/\/youtu\.be\//i,  // YouTube
  /https?:\/\/github\.com\/.*\/assets\//i,       // GitHub-hosted attachment
  /https?:\/\/user-images\.githubusercontent\.com\//i,            // GitHub user images
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

// Returns true when any change type that requires a demo is checked:
// Bug fix, Feature, or UI / frontend change.
function requiresDemo(body) {
  const text = body ?? "";
  return (
    /- \[[xX]\] Bug fix/.test(text) ||
    /- \[[xX]\] Feature/.test(text) ||
    /- \[[xX]\] UI \/ frontend change/.test(text)
  );
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
    .replace(/<!--[\s\S]*?(?:-->|$)/g, "")  // complete and unclosed HTML comments
    .trim();
}

// Returns true when the demo section contains real media (image/video/gif).
function hasDemoContent(body) {
  const content = extractDemoContent(body);
  if (!content) return false;
  return DEMO_MEDIA_PATTERNS.some((re) => re.test(content));
}

const demoRequiredMessage = (author) =>
  `@${author} This PR is a **Bug fix**, **Feature**, or **UI / frontend change** but the **Demo** section is missing or only contains a placeholder.

These change types require a screenshot or screen recording so reviewers can see the new behaviour without checking out the branch. Please update the **Demo** section with:

- A screenshot or screen recording of the change, or
- A link to a hosted video or GIF showing the new behaviour.

_Use \`N/A\` only when the change has no user-visible effect whatsoever (e.g. a pure refactor or test-only change). If that's the case, uncheck the relevant type box and check **Refactor / chore** or **Test / CI** instead._`;

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

    const cutoff = new Date(Date.now() - HOURS_TO_SCAN * MS_PER_HOUR);
    // GitHub search supports ISO 8601 timestamps for sub-day precision.
    const cutoffString = cutoff.toISOString().replace(/\.\d{3}Z$/, "Z");

    // Paginate a single ISSUE search to completion and return its PR nodes.
    async function fetchPRs(searchQuery) {
      console.log(`Scanning PRs: ${searchQuery}`);
      let cursor = null;
      let hasNextPage = true;
      const prs = [];
      while (hasNextPage) {
        const response = await github.graphql(QUERY, { cursor, searchQuery });
        const { remaining, resetAt } = response.rateLimit;
        console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
        const { nodes, pageInfo } = response.search;
        hasNextPage = pageInfo.hasNextPage;
        cursor = pageInfo.endCursor;
        prs.push(...nodes);
      }
      return prs;
    }

    // Two searches, unioned and deduped by PR number:
    //  1. Recently-opened PRs — the window that flags new PRs.
    //  2. Every open PR already labeled needs-demo, regardless of age, so the
    //     label can be cleared once a demo is added even on PRs older than the
    //     scan window.
    const recentQuery = `repo:${owner}/${repo} is:pr is:open created:>${cutoffString}`;
    const labeledQuery = `repo:${owner}/${repo} is:pr is:open label:${NEEDS_DEMO_LABEL}`;

    const byNumber = new Map();
    for (const pr of await fetchPRs(recentQuery)) byNumber.set(pr.number, pr);
    // Track which PRs the labeled search returned. Their `labels(first: 20)`
    // list can truncate and omit needs-demo on a PR with many labels, so the
    // search result is the source of truth for "is labeled", not that list.
    const labeledPRs = new Set();
    for (const pr of await fetchPRs(labeledQuery)) {
      byNumber.set(pr.number, pr);
      labeledPRs.add(pr.number);
    }
    const allPRs = [...byNumber.values()];

    console.log(`Found ${allPRs.length} unique open PR(s) to evaluate`);

    let flaggedCount = 0;
    let clearedCount = 0;
    let skippedCount = 0;

    for (const pr of allPRs) {
      // Skip drafts and maintainer PRs (by association and MAINTAINER file).
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

      // Trust the labeled search over the (possibly truncated) labels list:
      // a PR it returned is labeled even if needs-demo fell outside first: 20.
      const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
      const isLabeled = labeledPRs.has(pr.number) || labels.includes(NEEDS_DEMO_LABEL);
      // Requires a demo (Bug fix / Feature / UI) and none is present yet.
      const needsDemo = requiresDemo(pr.body) && !hasDemoContent(pr.body);

      // Already flagged: never comment again. Clear the label once the PR
      // satisfies the requirement — a demo was added, or the change-type box
      // was unchecked — so the label reflects the PR's current state rather
      // than its state at first scan.
      if (isLabeled) {
        if (!needsDemo) {
          try {
            await github.rest.issues.removeLabel({
              owner,
              repo,
              issue_number: pr.number,
              name: NEEDS_DEMO_LABEL,
            });
            console.log(`PR #${pr.number} (@${author}): demo satisfied — cleared '${NEEDS_DEMO_LABEL}'`);
            clearedCount++;
          } catch (err) {
            // 404 = label already gone; anything else is unexpected.
            if (err.status !== 404) {
              core.warning(`Could not remove '${NEEDS_DEMO_LABEL}' from #${pr.number}: ${err.message}`);
            }
          }
        }
        continue;
      }

      // Not yet flagged: nothing to do unless a demo is required and missing.
      if (!needsDemo) {
        continue;
      }

      console.log(`PR #${pr.number} (@${author}): demo required but not provided`);

      // Comment before labeling: if the comment fails the PR stays unlabeled
      // and will be retried on the next run. Labeling first would permanently
      // suppress the reminder on a transient comment failure.
      await github.rest.issues.createComment({
        owner,
        repo,
        issue_number: pr.number,
        body: demoRequiredMessage(author),
      });

      await github.rest.issues.addLabels({
        owner,
        repo,
        issue_number: pr.number,
        labels: [NEEDS_DEMO_LABEL],
      });

      flaggedCount++;
    }

    console.log(
      `Done. Flagged ${flaggedCount} PR(s); cleared ${clearedCount}; skipped ${skippedCount} (drafts / maintainers).`
    );
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log("Rate limit hit. Exiting gracefully.");
      return;
    }
    throw error;
  }
};
