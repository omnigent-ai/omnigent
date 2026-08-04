// Reopen a bot-closed PR when its author comments `/reopen`.
//
// Why this exists: reopening a PR needs Triage+ on the base repo, so a fork
// contributor (Read only) cannot undo a bot close themselves -- their only
// option today is opening a fresh PR. This lets them ask the bot, which does
// have the permission, to do it.
//
// Only the PR author may use it, and only on a PR a bot closed (never one a
// maintainer closed deliberately, and never a merged one). Reopening also
// requires the head branch to still exist; if it is gone, say so instead of
// failing silently.

const BOT_CLOSERS = ["github-actions[bot]"];

const notAuthor = () =>
  "Only the PR author can use `/reopen`. A maintainer can reopen this PR directly.";

const closedByHuman = (login) =>
  `This PR was closed by @${login}, not automatically, so \`/reopen\` does not apply. ` +
  `Please reply here and ask them to reopen it.`;

const branchGone = (ref) =>
  `Cannot reopen: the source branch \`${ref}\` no longer exists. ` +
  `Push it again and open a fresh PR referencing this one.`;

const reopened = () => "Reopened. Thanks for following up!";

// The actor that performed the most recent close. Null when nothing closed it.
async function lastCloser({ github, owner, repo, number }) {
  const events = await github.paginate(github.rest.issues.listEventsForTimeline, {
    owner,
    repo,
    issue_number: number,
    per_page: 100,
  });
  const closes = events.filter((e) => e.event === "closed");
  return closes.length ? closes[closes.length - 1].actor?.login ?? null : null;
}

module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const number = context.payload.issue.number;
  const commenter = context.payload.comment.user.login;

  const comment = async (body) =>
    github.rest.issues.createComment({ owner, repo, issue_number: number, body });

  const pr = (await github.rest.pulls.get({ owner, repo, pull_number: number })).data;

  if (pr.merged) {
    core.info(`PR #${number} is merged; ignoring.`);
    return;
  }
  if (pr.state === "open") {
    core.info(`PR #${number} is already open; ignoring.`);
    return;
  }
  if (commenter !== pr.user.login) {
    await comment(notAuthor());
    return;
  }

  const closer = await lastCloser({ github, owner, repo, number });
  if (closer && !BOT_CLOSERS.includes(closer)) {
    await comment(closedByHuman(closer));
    return;
  }

  // A fork whose branch (or whole repo) is gone leaves head.repo null or the
  // ref unresolvable -- GitHub then refuses the reopen.
  if (!pr.head.repo) {
    await comment(branchGone(pr.head.ref));
    return;
  }
  try {
    await github.rest.repos.getBranch({
      owner: pr.head.repo.owner.login,
      repo: pr.head.repo.name,
      branch: pr.head.ref,
    });
  } catch (err) {
    if (err.status === 404) {
      await comment(branchGone(pr.head.ref));
      return;
    }
    throw err;
  }

  await github.rest.pulls.update({ owner, repo, pull_number: number, state: "open" });
  await comment(reopened());
  core.info(`Reopened PR #${number} for @${commenter}.`);
};
