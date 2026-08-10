// Tell the author how to reopen, on every close that leaves `/reopen` usable.
//
// Bot closers post their own tailored notice (see duplicate-prs.js), and GitHub
// suppresses the `closed` event for GITHUB_TOKEN-driven closes anyway, so in
// practice this covers human closes: a maintainer closing a community PR, or an
// author closing their own. Merges are not closes. A maintainer's close is
// deliberate, so the author is pointed at the maintainer rather than at
// `/reopen`, which would refuse them anyway.
//
// Posts at most once per PR: a PR closed, reopened, and closed again does not
// re-notify.

const MARKER = "<!-- reopen-notice -->";

const authorClosed = () =>
  `${MARKER}\nClosed. If you want to pick this back up, comment \`/reopen\`. ` +
  `GitHub only lets maintainers press the Reopen button, so this command does it for you. ` +
  `It needs the source branch to still exist.`;

const maintainerClosed = (author) =>
  `${MARKER}\n@${author} this PR was closed by a maintainer. If you think that was a mistake, ` +
  `reply here and ask them to reopen it. \`/reopen\` only undoes automated closes. ` +
  `See [CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md#reopening-a-closed-pr).`;

module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;

  if (pr.merged) {
    core.info(`PR #${pr.number} was merged, not closed; nothing to say.`);
    return;
  }

  const closer = context.payload.sender.login;
  if (closer.endsWith("[bot]")) {
    core.info(`PR #${pr.number} closed by ${closer}, which posts its own notice.`);
    return;
  }

  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number: pr.number,
    per_page: 100,
  });
  if (comments.some((c) => c.body?.includes(MARKER))) {
    core.info(`PR #${pr.number} already has the reopen notice.`);
    return;
  }

  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pr.number,
    body: closer === pr.user.login ? authorClosed() : maintainerClosed(pr.user.login),
  });
  core.info(`Posted reopen notice on #${pr.number} (closed by ${closer}).`);
};
