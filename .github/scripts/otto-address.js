// Validate against GitHub and main before using the private-repository token.
module.exports = async ({ github, context, core, reviewers = "" }) => {
  const { owner, repo } = context.repo;
  if (`${owner}/${repo}` !== "omnigent-ai/omnigent" || !context.payload.issue?.pull_request) return;
  const issue_number = context.payload.issue.number;
  const comment_id = context.payload.comment.id;
  const { data: comment } = await github.rest.issues.getComment({ owner, repo, comment_id });
  if (
    comment.body.trim() !== "/address" ||
    comment.user.type !== "User" ||
    comment.issue_url !== `https://api.github.com/repos/${owner}/${repo}/issues/${issue_number}`
  ) return;
  const allowed = new Set(reviewers.toLowerCase().split(/[,\s]+/).filter(Boolean));
  const { data: file } = await github.rest.repos.getContent({
    owner, repo, path: ".github/MAINTAINER", ref: "main",
  });
  for (const line of Buffer.from(file.content, "base64").toString("utf8").split("\n")) {
    const login = line.split("#")[0].trim().toLowerCase();
    if (login) allowed.add(login);
  }
  if (!allowed.has(comment.user.login.toLowerCase())) {
    core.info("/address is limited to maintainers and configured Otto reviewers.");
    return;
  }
  if (comment.created_at !== comment.updated_at) {
    core.setOutput("reason", "Edited commands are not accepted. Post a new /address comment.");
    return;
  }
  const { data: pull } = await github.rest.pulls.get({ owner, repo, pull_number: issue_number });
  if (
    pull.state !== "open" || pull.draft ||
    pull.user.login !== "omni-resolve-agent[bot]" || pull.user.type !== "Bot" ||
    pull.head.repo?.full_name !== `${owner}/${repo}` ||
    !pull.head.ref.startsWith("resolve-agent/") || pull.head.ref === pull.base.ref
  ) {
    core.setOutput("reason", "/address requires an open, non-draft Resolve-authored PR on its own branch.");
    return;
  }
  core.setOutput("authorized", "true");
};
