const CURRENT_PULL_REQUEST = `
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        state
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

// The caller holds the same per-head concurrency lock as full gate evaluations.
async function expire({ github, context, core, pr, sha, now = Date.now() }) {
  const { owner, repo } = context.repo;
  const { repository } = await github.graphql(CURRENT_PULL_REQUEST, { owner, repo, pr });
  const pullRequest = repository.pullRequest;
  if (!pullRequest || pullRequest.state !== "OPEN" || pullRequest.headRefOid !== sha) {
    core.info("Skipped expiry: PR closed or head changed.");
    return false;
  }
  const metadata = refreshMetadata(pullRequest, now);
  if (!metadata?.expired) {
    core.info("Skipped expiry: latest status is not green with an expired deadline.");
    return false;
  }
  await github.rest.repos.createCommitStatus({
    owner, repo, sha,
    state: "failure",
    context: "Merge Ready",
    description: "CI freshness not satisfied (24h); update branch and run fresh CI",
  });
  core.info(`Expired Merge Ready for #${pr} (${sha}): ${metadata.reason}`);
  return true;
}

module.exports = { refreshMetadata, expire };
