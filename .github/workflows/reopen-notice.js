// One closure/reopen notice per PR. Otto feedback stays in the same comment.
const MARKER = "<!-- reopen-notice -->";
const OTTO_AUTHORS = new Set([
  "app/omni-resolve-agent",
  "omni-resolve-agent",
  "omni-resolve-agent[bot]",
]);
const MAINTAINERS = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);
const isBot = (user) => user?.type === "Bot" || user?.login?.endsWith("[bot]");

const authorClosed = () =>
  `${MARKER}\nClosed. If you want to pick this back up, comment \`/reopen\`. ` +
  `GitHub only lets maintainers press the Reopen button, so this command does it for you. ` +
  `It needs the source branch to still exist.`;

const maintainerClosed = (author) =>
  `${MARKER}\n@${author} this PR was closed by a maintainer. If you think that was a mistake, ` +
  `reply here and ask them to reopen it. \`/reopen\` only undoes automated closes. ` +
  `See [CONTRIBUTING.md](https://github.com/omnigent-ai/omnigent/blob/main/CONTRIBUTING.md#reopening-a-closed-pr).`;

const feedbackRequest = (closer) =>
  `@${closer}, could you leave a one-line reason for closing this Otto PR? ` +
  `An optional category is enough: wrong root cause, design conflict, wrong mechanism, ` +
  `missed surface, not needed, or other (with a few words of context). ` +
  `If you explained elsewhere, a link is enough.`;

function visibleText(body) {
  return String(body ?? "")
    // Separate surrounding text so removing a hidden span cannot join tokens.
    .replace(/<!--[\s\S]*?-->|```[\s\S]*?```|~~~[\s\S]*?~~~/g, " ")
    .split("\n")
    .filter((line) => !/^\s*(>|\/)/.test(line))
    .join(" ")
    .replace(/[*_`]/g, "")
    .trim();
}

// Prefer suppressing a request to asking a maintainer to repeat prior feedback.
function hasExplanation(entry, closer) {
  const body = entry.body ?? "";
  if (body.includes(MARKER)) return false;
  const text = visibleText(body);
  if (!text) return false;
  if (/^(?:lgtm|thanks(?: you)?|thank you|looks good(?: to me)?|approved|done|closing|closed|\/\w+)[\s.!👍]*$/i.test(text)) {
    return false;
  }
  const explicitReason = /\b(wrong root cause|design conflict|conflicts? with (?:the )?design|wrong mechanism|missed surface|not needed|not (?:a |the )?(?:bug|root cause)|supersed\w*|replaced by|replacement #\d+|duplicate of|as (?:a )?duplicate|clos(?:e|ed|ing) [^.!\n]*because|does(?: not|n't|n’t) fix|already (?:fixed|resolved)|no longer (?:needed|relevant))\b/i.test(text);
  // Automation may already explain a stale/superseded PR. It is never tagged.
  if (isBot(entry.user)) return explicitReason;
  if (entry.user?.login !== closer && !MAINTAINERS.has(entry.author_association)) {
    return false;
  }
  // Any substantive maintainer feedback counts, including terse free-form
  // reasons and links. We do not infer a category or ingest the response.
  return /[\p{L}\p{N}]/u.test(text);
}

module.exports = async ({ github, context, core }) => {
  const { owner, repo } = context.repo;
  const pr = context.payload.pull_request;

  if (pr.merged) {
    core.info(`PR #${pr.number} was merged, not closed; nothing to say.`);
    return;
  }

  const sender = context.payload.sender;
  if (isBot(sender)) {
    core.info(`PR #${pr.number} closed by ${sender.login}, which posts its own notice.`);
    return;
  }

  const params = { owner, repo, issue_number: pr.number, per_page: 100 };
  const comments = await github.paginate(github.rest.issues.listComments, params);
  if (comments.some((c) => c.body?.includes(MARKER))) {
    core.info(`PR #${pr.number} already has the reopen notice.`);
    return;
  }

  let closer = sender.login;
  let ask = false;
  if (OTTO_AUTHORS.has(pr.user.login)) {
    const { data: current } = await github.rest.pulls.get({
      owner, repo, pull_number: pr.number,
    });
    if (current.state !== "closed" || current.merged || current.closed_at !== pr.closed_at) {
      core.info(`PR #${pr.number} changed since this close; leaving it alone.`);
      return;
    }
    const events = await github.paginate(github.rest.issues.listEventsForTimeline, params);
    const closed = events.filter((event) => event.event === "closed").at(-1);
    // Use the actual closing actor, never the workflow rerunner or PR author.
    if (!closed?.actor?.login || closed.created_at !== pr.closed_at || isBot(closed.actor)) {
      core.info(`PR #${pr.number} has no matching human close; leaving it alone.`);
      return;
    }
    closer = closed.actor.login;
    const reviewParams = { owner, repo, pull_number: pr.number, per_page: 100 };
    const reviews = await github.paginate(github.rest.pulls.listReviews, reviewParams);
    const reviewComments = await github.paginate(github.rest.pulls.listReviewComments, reviewParams);
    ask = ![...comments, ...reviews, ...reviewComments].some((entry) => hasExplanation(entry, closer));
    const { data: latest } = await github.rest.pulls.get({
      owner, repo, pull_number: pr.number,
    });
    if (latest.state !== "closed" || latest.merged || latest.closed_at !== pr.closed_at) {
      core.info(`PR #${pr.number} changed while reading feedback; leaving it alone.`);
      return;
    }
  }

  const notice = closer === pr.user.login ? authorClosed() : maintainerClosed(pr.user.login);
  await github.rest.issues.createComment({
    owner,
    repo,
    issue_number: pr.number,
    body: ask ? `${notice}\n\n${feedbackRequest(closer)}` : notice,
    // A lost POST response may already have posted. Redelivery checks MARKER.
    request: { retries: 0 },
  });
  core.info(`Posted reopen notice on #${pr.number} (closed by ${closer}, feedback requested: ${ask}).`);
};
