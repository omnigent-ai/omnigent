// Validate against GitHub and main before using the private-repository token.
module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  if (`${owner}/${repo}` !== "omnigent-ai/omnigent" || !context.payload.issue?.pull_request) return;
  const issue_number = context.payload.issue.number;
  const comment_id = context.payload.comment.id;
  const { data: comment } = await github.rest.issues.getComment({ owner, repo, comment_id });
  if (
    comment.body.trim() !== "/address" ||
    comment.user.type !== "User" ||
    comment.issue_url !== `https://api.github.com/repos/${owner}/${repo}/issues/${issue_number}` ||
    comment.created_at !== comment.updated_at
  ) return;
  const { data: pull } = await github.rest.pulls.get({ owner, repo, pull_number: issue_number });
  if (
    pull.state !== "open" || pull.draft ||
    pull.user.login !== "omni-resolve-agent[bot]" || pull.user.type !== "Bot" ||
    pull.head.repo?.full_name !== `${owner}/${repo}` ||
    !pull.head.ref.startsWith("resolve-agent/") || pull.head.ref === pull.base.ref
  ) return;
  const allowed = new Set();
  for (const path of [".github/MAINTAINER", ".github/OTTO_REVIEWERS"]) {
    const { data: file } = await github.rest.repos.getContent({ owner, repo, path, ref: "main" });
    for (const line of Buffer.from(file.content, "base64").toString("utf8").split("\n")) {
      const login = line.split("#")[0].trim().toLowerCase();
      if (login) allowed.add(login);
    }
  }
  if (!allowed.has(comment.user.login.toLowerCase())) {
    core.info("/address is limited to maintainers and configured Otto reviewers.");
    return;
  }
  core.setOutput("authorized", "true");
};
