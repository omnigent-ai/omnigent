const TRUSTED_REPLACEMENT_AUTHORS = new Set([
  "app/omni-resolve-agent",
  "omni-resolve-agent",
  "omni-resolve-agent[bot]",
]);

const supersededPRNumber = (body) => {
  const match = String(body || "").match(/^Supersedes #(\d+)\b/im);
  return match ? Number(match[1]) : null;
};

module.exports = async ({ context, github }) => {
  const replacement = context.payload.pull_request;
  if (!replacement?.merged) return;

  const author = replacement.user?.login ?? "";
  if (!TRUSTED_REPLACEMENT_AUTHORS.has(author)) return;

  const oldNumber = supersededPRNumber(replacement.body);
  if (!oldNumber || oldNumber === replacement.number) return;

  const { owner, repo } = context.repo;
  const { data: oldPR } = await github.rest.pulls.get({
    owner,
    repo,
    pull_number: oldNumber,
  });
  if (oldPR.state !== "open") return;

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: oldNumber,
    body:
      `Replacement #${replacement.number} has merged. Closing this PR now that ` +
      "the credited replacement is on the default branch.",
  });
  await github.rest.pulls.update({
    owner,
    repo,
    pull_number: oldNumber,
    state: "closed",
  });
};

module.exports.supersededPRNumber = supersededPRNumber;
