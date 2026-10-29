const OPEN_PULL_REQUESTS = `
  query($owner: String!, $repo: String!, $cursor: String) {
    repository(owner: $owner, name: $repo) {
      defaultBranchRef { name }
      pullRequests(first: 100, after: $cursor, states: OPEN) {
        nodes {
          number
          headRefOid
          commits(last: 1) {
            nodes {
              commit {
                oid
                status { contexts { context state description } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
`;

function refreshMetadata(pullRequest, now) {
  const commit = pullRequest.commits?.nodes?.[0]?.commit;
  if (!commit || commit.oid !== pullRequest.headRefOid) return null;

  const status = commit.status?.contexts?.find(
    (entry) => entry.context === "Merge Ready",
  );
  if (status?.state !== "SUCCESS") return null;

  const match = /^All required checks green; expires (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)$/.exec(
    status.description ?? "",
  );
  const expiry = match && Date.parse(match[1]);
  if (
    !match ||
    !Number.isFinite(expiry) ||
    new Date(expiry).toISOString().replace(".000Z", "Z") !== match[1]
  ) {
    return { reason: "green status has no valid CI expiry", expired: false };
  }
  return expiry <= now ? { reason: `CI expired at ${match[1]}`, expired: true } : null;
}

module.exports = async function sweep({
  github,
  context,
  core,
  dryRun = true,
  now = Date.now(),
  maxDispatches = 50,
}) {
  if (!Number.isInteger(maxDispatches) || maxDispatches < 1) {
    throw new Error("maxDispatches must be a positive integer");
  }
  const { owner, repo } = context.repo;
  const expiredCandidates = [];
  const legacyCandidates = [];
  let cursor = null;
  let defaultBranch;
  let scanned = 0;
  do {
    const { repository } = await github.graphql(OPEN_PULL_REQUESTS, {
      owner,
      repo,
      cursor,
    });
    defaultBranch = repository.defaultBranchRef.name;
    const { nodes, pageInfo } = repository.pullRequests;
    scanned += nodes.length;
    for (const pullRequest of nodes) {
      const metadata = refreshMetadata(pullRequest, now);
      if (metadata) {
        const queue = metadata.expired ? expiredCandidates : legacyCandidates;
        queue.push({
          number: pullRequest.number,
          sha: pullRequest.headRefOid,
          reason: metadata.reason,
        });
      }
    }
    if (!pageInfo.hasNextPage) break;
    if (!pageInfo.endCursor || pageInfo.endCursor === cursor) {
      throw new Error("Open pull request pagination did not advance");
    }
    cursor = pageInfo.endCursor;
  } while (true);

  // Existing deadlines take priority over the one-time legacy-status backfill.
  const candidates = [...expiredCandidates, ...legacyCandidates];
  const deferred = Math.max(0, candidates.length - maxDispatches);
  core.info(
    `${dryRun ? "Dry run: " : ""}scanned ${scanned} open PRs; ${candidates.length} need Merge Ready reevaluation; ${deferred} deferred beyond the ${maxDispatches}-attempt limit`,
  );
  let dispatched = 0;
  const failures = [];
  const selectedCandidates = dryRun ? candidates : candidates.slice(0, maxDispatches);
  for (const candidate of selectedCandidates) {
    core.info(`#${candidate.number} (${candidate.sha}): ${candidate.reason}`);
    if (dryRun) continue;
    try {
      await github.rest.actions.createWorkflowDispatch({
        owner,
        repo,
        workflow_id: "merge-ready.yml",
        ref: defaultBranch,
        inputs: { pr: String(candidate.number), sha: candidate.sha },
      });
      dispatched += 1;
    } catch (error) {
      failures.push(candidate.number);
      core.warning(`Could not reevaluate #${candidate.number}: ${error.message}`);
    }
  }
  if (failures.length) {
    throw new Error(`Failed to dispatch Merge Ready for PRs: ${failures.join(", ")}`);
  }
  return { scanned, candidates, dispatched, deferred };
};
