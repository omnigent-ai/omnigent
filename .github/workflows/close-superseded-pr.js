const TRUSTED_REPLACEMENT_AUTHORS = new Set([
  "app/omni-resolve-agent",
  "omni-resolve-agent",
  "omni-resolve-agent[bot]",
]);

function supersededPRNumbers(body) {
  return [
    ...new Set(
      [...String(body || "").matchAll(/^Supersedes #([1-9][0-9]*)\b/gim)]
        .map((match) => Number(match[1]))
        .filter(Number.isSafeInteger),
    ),
  ];
}

function trustedReplacement(pr, repository, defaultBranch) {
  return (
    pr?.user?.type === "Bot" &&
    TRUSTED_REPLACEMENT_AUTHORS.has(pr.user.login.toLowerCase()) &&
    pr.merged === true &&
    pr.draft === false &&
    Number.isFinite(Date.parse(pr.merged_at)) &&
    pr.base?.repo?.full_name?.toLowerCase() === repository.toLowerCase() &&
    pr.base.ref === defaultBranch
  );
}

const SOURCE_FIELDS = `
  __typename ... on PullRequest {
    number state isDraft body mergedAt baseRefName
    author {login __typename} repository {nameWithOwner}
  }
`;
const TIMELINE_FIELDS = `
  pageInfo {hasNextPage endCursor}
  nodes { ... on CrossReferencedEvent { source { ${SOURCE_FIELDS} } } }
`;
const OPEN_QUERY = `query($owner:String!,$repo:String!,$after:String) {
  repository(owner:$owner,name:$repo) {
    pullRequests(states:OPEN,first:25,after:$after,orderBy:{field:CREATED_AT,direction:ASC}) {
      pageInfo {hasNextPage endCursor}
      nodes {number timelineItems(first:100,itemTypes:[CROSS_REFERENCED_EVENT]) {
        ${TIMELINE_FIELDS}
      }}
    }
  }
}`;
const TIMELINE_QUERY = `query($owner:String!,$repo:String!,$number:Int!,$after:String!) {
  repository(owner:$owner,name:$repo) {
    pullRequest(number:$number) {
      timelineItems(first:100,after:$after,itemTypes:[CROSS_REFERENCED_EVENT]) {
        ${TIMELINE_FIELDS}
      }
    }
  }
}`;

function nextCursor(page, seen) {
  if (page.hasNextPage === false) return null;
  if (
    page.hasNextPage !== true ||
    !page.endCursor ||
    seen.has(page.endCursor)
  ) {
    throw new Error("Incomplete GitHub pagination");
  }
  seen.add(page.endCursor);
  return page.endCursor;
}

function restSource(source) {
  return {
    number: source.number,
    body: source.body,
    merged: source.state === "MERGED",
    draft: source.isDraft,
    merged_at: source.mergedAt,
    user: { login: source.author?.login, type: source.author?.__typename },
    base: {
      ref: source.baseRefName,
      repo: { full_name: source.repository?.nameWithOwner },
    },
  };
}

async function discover({
  github,
  owner,
  repo,
  defaultBranch,
  report,
  handle,
}) {
  const seen = new Set();
  let after = null;
  do {
    const data = await github.graphql(OPEN_QUERY, { owner, repo, after });
    const page = data.repository.pullRequests;
    for (const pull of page.nodes) {
      report.inspected += 1;
      let timeline = pull.timelineItems;
      const cursors = new Set();
      const sources = new Set();
      try {
        while (true) {
          for (const event of timeline.nodes) {
            const source = event.source;
            if (!source) {
              report.results.push({
                old: pull.number,
                status: "unknown",
                reason: "source_unavailable",
              });
              continue;
            }
            if (source.__typename !== "PullRequest") continue;
            const pr = restSource(source);
            if (
              !trustedReplacement(pr, `${owner}/${repo}`, defaultBranch) ||
              !supersededPRNumbers(pr.body).includes(pull.number) ||
              sources.has(pr.number)
            )
              continue;
            sources.add(pr.number);
            await handle(pull.number, pr.number);
          }
          const cursor = nextCursor(timeline.pageInfo, cursors);
          if (!cursor) break;
          const next = await github.graphql(TIMELINE_QUERY, {
            owner,
            repo,
            number: pull.number,
            after: cursor,
          });
          timeline = next.repository.pullRequest.timelineItems;
        }
      } catch {
        report.results.push({
          old: pull.number,
          status: "unknown",
          reason: "timeline_unavailable",
        });
      }
    }
    after = nextCursor(page.pageInfo, seen);
  } while (after);
}

async function closeOne({
  github,
  owner,
  repo,
  defaultBranch,
  oldNumber,
  replacementNumber,
  dryRun,
}) {
  const result = {
    old: oldNumber,
    replacement: replacementNumber,
    replacement_url: `https://github.com/${owner}/${repo}/pull/${replacementNumber}`,
  };
  const skip = (reason) => ({ ...result, status: "skipped", reason });
  if (oldNumber === replacementNumber) return skip("self_reference");
  const get = async (number) =>
    (await github.rest.pulls.get({ owner, repo, pull_number: number })).data;
  const valid = (pr) =>
    trustedReplacement(pr, `${owner}/${repo}`, defaultBranch) &&
    supersededPRNumbers(pr.body).includes(oldNumber);
  let replacement = await get(replacementNumber);
  if (!valid(replacement)) return skip("replacement_no_longer_qualifies");
  let oldPR = await get(oldNumber);
  if (oldPR.state !== "open") return skip("already_closed");
  const params = { owner, repo, issue_number: oldNumber, per_page: 100 };
  const marker = `<!-- omnigent-superseded-by:${replacementNumber} -->`;
  const explanation =
    `Replacement #${replacementNumber} has merged. Closing this PR now that ` +
    "the credited replacement is on the default branch.";
  const comments = await github.paginate(
    github.rest.issues.listComments,
    params,
  );
  const commented = comments.some(
    (c) =>
      c.user?.type === "Bot" &&
      c.user.login === "github-actions[bot]" &&
      (c.body?.includes(marker) || c.body === explanation),
  );
  const events = await github.paginate(
    github.rest.issues.listEventsForTimeline,
    params,
  );
  // Re-read both PRs after paginated reads, immediately before any writes.
  replacement = await get(replacementNumber);
  oldPR = await get(oldNumber);
  if (!valid(replacement)) return skip("replacement_no_longer_qualifies");
  if (oldPR.state !== "open") return skip("already_closed");
  result.replacement_head = replacement.head.sha;
  result.old_head = oldPR.head.sha;
  if (
    events.some(
      (e) =>
        e.event === "reopened" &&
        (!Number.isFinite(Date.parse(e.created_at)) ||
          Date.parse(e.created_at) >= Date.parse(replacement.merged_at)),
    )
  ) {
    return skip("reopened_after_replacement");
  }
  if (dryRun)
    return { ...result, status: "would_close", reason: "merged_replacement" };
  if (!commented) {
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: oldNumber,
      body: `${explanation}\n\n${marker}`,
    });
  }
  await github.rest.pulls.update({
    owner,
    repo,
    pull_number: oldNumber,
    state: "closed",
  });
  return { ...result, status: "closed", reason: "merged_replacement" };
}

module.exports = async ({
  context,
  github,
  dryRun = true,
  replacementNumber = null,
}) => {
  const { owner, repo } = context.repo;
  const report = {
    dry_run: dryRun,
    observed_at: new Date().toISOString(),
    inspected: 0,
    results: [],
  };
  const handle = async (oldNumber, sourceNumber) => {
    try {
      report.results.push(
        await closeOne({
          github,
          owner,
          repo,
          defaultBranch,
          oldNumber,
          replacementNumber: sourceNumber,
          dryRun,
        }),
      );
    } catch {
      report.results.push({
        old: oldNumber,
        replacement: sourceNumber,
        replacement_url: `https://github.com/${owner}/${repo}/pull/${sourceNumber}`,
        status: "unknown",
        reason: "read_or_write_failed",
      });
    }
  };
  let defaultBranch;
  try {
    defaultBranch = (await github.rest.repos.get({ owner, repo })).data
      .default_branch;
    if (!defaultBranch) throw new Error("Missing default branch");
    const number = replacementNumber ?? context.payload.pull_request?.number;
    if (number != null) {
      if (!Number.isSafeInteger(number) || number < 1)
        throw new Error("Invalid replacement number");
      const { data: pr } = await github.rest.pulls.get({
        owner,
        repo,
        pull_number: number,
      });
      if (!trustedReplacement(pr, `${owner}/${repo}`, defaultBranch)) {
        report.results.push({
          replacement: number,
          status: "skipped",
          reason: "replacement_not_merged_or_trusted",
        });
      } else {
        for (const oldNumber of supersededPRNumbers(pr.body))
          await handle(oldNumber, number);
      }
    } else {
      const candidates = [];
      await discover({
        github,
        owner,
        repo,
        defaultBranch,
        report,
        handle: async (old, replacement) => candidates.push([old, replacement]),
      });
      // Finish pagination before closures change the set of open PRs.
      for (const [old, replacement] of candidates)
        await handle(old, replacement);
    }
  } catch {
    report.results.push({ status: "unknown", reason: "discovery_failed" });
  }
  report.counts = report.results.reduce((counts, row) => {
    counts[row.status] = (counts[row.status] || 0) + 1;
    return counts;
  }, {});
  return report;
};

module.exports.supersededPRNumbers = supersededPRNumbers;
