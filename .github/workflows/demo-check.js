// Keep `needs-demo` aligned with the current PR. PR_NUMBER checks one PR after
// a lifecycle event; without it, a manual run also repairs existing labels.

const MS_PER_HOUR = 60 * 60 * 1000;
const HOURS_TO_SCAN = 24;
const NEEDS_DEMO_LABEL = "needs-demo";
const DEMO_COMMENT_MARKER = "<!-- needs-demo-comment -->";
const LEGACY_DEMO_COMMENT_TEXT =
  "This PR is a **Bug fix**, **Feature**, or **UI / frontend change** but the **Demo** section is missing";

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
          state
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

// Visual media is required only when UI / frontend change is checked.
function requiresDemo(body) {
  const text = body ?? "";
  return /- \[[xX]\] UI \/ frontend change/.test(text);
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

function bodyNeedsDemo(body) {
  return requiresDemo(body) && !hasDemoContent(body);
}

const demoRequiredMessage = (author) =>
  `${DEMO_COMMENT_MARKER}
@${author} This PR checks **UI / frontend change**, but the **Demo** section has no screenshot or recording.

UI / frontend changes require visual evidence so reviewers can see the new behaviour without checking out the branch. Please update the **Demo** section with:

- A screenshot or screen recording of the change, or
- A link to a hosted video or GIF showing the new behaviour.

_If this PR has no visual surface, uncheck **UI / frontend change** and provide non-visual evidence in the **Test Plan**._`;

const demoResolvedMessage = `${DEMO_COMMENT_MARKER}
✅ This PR no longer requires demo follow-up.`;

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
        description: "UI PR needs a demo screenshot or recording",
      });
    } catch (err) {
      // 422 = already exists; anything else is unexpected.
      if (err.status !== 422) {
        core.warning(`Could not create label '${NEEDS_DEMO_LABEL}': ${err.message}`);
      }
    }

    const findDemoComment = async (issueNumber) => {
      const comments = await github.paginate(github.rest.issues.listComments, {
        owner,
        repo,
        issue_number: issueNumber,
        per_page: 100,
      });
      return comments.find(
        (comment) =>
          comment.user?.type === "Bot" &&
          (comment.body?.includes(DEMO_COMMENT_MARKER) ||
            comment.body?.includes(LEGACY_DEMO_COMMENT_TEXT))
      );
    };

    const allPRs = new Map();
    const labeledPRs = new Set();
    const single = Number(process.env.PR_NUMBER) || null;

    if (single) {
      const response = await github.rest.pulls.get({ owner, repo, pull_number: single });
      const pr = response.data;
      allPRs.set(pr.number, {
        number: pr.number,
        state: pr.merged ? "MERGED" : pr.state.toUpperCase(),
        author: { login: pr.user?.login },
        authorAssociation: pr.author_association,
        isDraft: pr.draft,
        labels: { nodes: pr.labels },
        body: pr.body,
      });
      console.log(`Checking PR #${single}`);
    } else {
      const fetchPRs = async (searchQuery) => {
        console.log(`Scanning PRs: ${searchQuery}`);
        const found = [];
        let cursor = null;
        let hasNextPage = true;
        while (hasNextPage) {
          const response = await github.graphql(QUERY, { cursor, searchQuery });
          const { remaining, resetAt } = response.rateLimit;
          console.log(`Rate limit: ${remaining} remaining, resets at ${resetAt}`);
          const { nodes, pageInfo } = response.search;
          found.push(...nodes);
          hasNextPage = pageInfo.hasNextPage;
          cursor = pageInfo.endCursor;
        }
        return found;
      };

      const cutoff = new Date(Date.now() - HOURS_TO_SCAN * MS_PER_HOUR);
      const cutoffString = cutoff.toISOString().replace(/\.\d{3}Z$/, "Z");
      const recentQuery = `repo:${owner}/${repo} is:pr is:open created:>${cutoffString}`;
      const labeledQuery = `repo:${owner}/${repo} is:pr label:${NEEDS_DEMO_LABEL}`;

      for (const pr of await fetchPRs(recentQuery)) allPRs.set(pr.number, pr);
      for (const pr of await fetchPRs(labeledQuery)) {
        allPRs.set(pr.number, pr);
        labeledPRs.add(pr.number);
      }
      console.log(`Found ${allPRs.size} PR(s) to check`);
    }

    let flaggedCount = 0;
    let clearedCount = 0;
    let skippedCount = 0;

    for (const pr of allPRs.values()) {
      const author = pr.author?.login ?? "contributor";
      const labels = pr.labels?.nodes?.map((l) => l.name) ?? [];
      const isLabeled = labeledPRs.has(pr.number) || labels.includes(NEEDS_DEMO_LABEL);
      const isMaintainer =
        MAINTAINER_ASSOCIATIONS.includes(pr.authorAssociation) ||
        maintainers.has(author.toLowerCase());
      const needsDemo =
        pr.state === "OPEN" &&
        !pr.isDraft &&
        !isMaintainer &&
        bodyNeedsDemo(pr.body);

      if (!needsDemo && isLabeled) {
        try {
          const existing = await findDemoComment(pr.number);
          if (existing && existing.body !== demoResolvedMessage) {
            await github.rest.issues.updateComment({
              owner,
              repo,
              comment_id: existing.id,
              body: demoResolvedMessage,
            });
          }
        } catch (err) {
          if (err.status === 429 || err.message?.includes("rate limit")) throw err;
          core.warning(`Could not resolve the demo reminder on #${pr.number}: ${err.message}`);
        }
        try {
          await github.rest.issues.removeLabel({
            owner,
            repo,
            issue_number: pr.number,
            name: NEEDS_DEMO_LABEL,
          });
          console.log(`PR #${pr.number}: removed '${NEEDS_DEMO_LABEL}'`);
          clearedCount++;
        } catch (err) {
          // A concurrent run may already have removed it.
          if (err.status !== 404) throw err;
        }
        continue;
      }

      if (!needsDemo || isLabeled) {
        skippedCount++;
        continue;
      }

      console.log(`PR #${pr.number} (@${author}): demo required but not provided`);

      const body = demoRequiredMessage(author);
      const existing = await findDemoComment(pr.number);

      if (!existing) {
        await github.rest.issues.createComment({
          owner,
          repo,
          issue_number: pr.number,
          body,
        });
      } else if (existing.body !== body) {
        await github.rest.issues.updateComment({
          owner,
          repo,
          comment_id: existing.id,
          body,
        });
      }

      await github.rest.issues.addLabels({
        owner,
        repo,
        issue_number: pr.number,
        labels: [NEEDS_DEMO_LABEL],
      });

      flaggedCount++;
    }

    console.log(
      `Done. Flagged ${flaggedCount} PR(s); cleared ${clearedCount}; skipped ${skippedCount}.`
    );
  } catch (error) {
    if (error.status === 429 || error.message?.includes("rate limit")) {
      console.log("Rate limit hit. Exiting gracefully.");
      return;
    }
    throw error;
  }
};

module.exports.bodyNeedsDemo = bodyNeedsDemo;
