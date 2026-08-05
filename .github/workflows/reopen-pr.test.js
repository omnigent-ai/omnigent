// Local unit test for reopen-pr.js -- mocks the GitHub client and runs the real
// decision logic. No network.

const assert = require("assert");
const path = require("path");
const script = require(path.resolve(".github/workflows/reopen-pr.js"));

// Run the script against a scenario; returns the side effects.
async function run({
  commenter = "ext",
  author = "ext",
  state = "closed",
  merged = false,
  closer = "github-actions[bot]",
  headRepo = { owner: { login: "ext" }, name: "omnigent" },
  branchExists = true,
  body = "/reopen",
}) {
  const reopens = [];
  const comments = [];
  const github = {
    paginate: async () => (closer ? [{ event: "closed", actor: { login: closer } }] : []),
    rest: {
      issues: {
        listEventsForTimeline: "listEventsForTimeline",
        createComment: async ({ body }) => comments.push(body),
      },
      pulls: {
        get: async () => ({ data: { state, merged, user: { login: author }, head: { ref: "feat", repo: headRepo } } }),
        update: async ({ pull_number, state }) => reopens.push({ pull_number, state }),
      },
      repos: {
        getBranch: async () => {
          if (!branchExists) {
            const err = new Error("Branch not found");
            err.status = 404;
            throw err;
          }
          return { data: {} };
        },
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: {
      issue: { number: 7, pull_request: {} },
      comment: { user: { login: commenter }, body },
    },
  };
  await script({ github, context, core: { info: () => {} } });
  return { reopens, comments };
}

(async () => {
  // Author reopening a bot-closed PR: reopened, with confirmation.
  let r = await run({});
  assert.deepStrictEqual(r.reopens, [{ pull_number: 7, state: "open" }]);
  assert.match(r.comments[0], /Reopened/);

  // Someone other than the author: refused, no reopen.
  r = await run({ commenter: "stranger" });
  assert.deepStrictEqual(r.reopens, []);
  assert.match(r.comments[0], /Only the PR author/);

  // Closed by a maintainer: refused, names them.
  r = await run({ closer: "maintainer1" });
  assert.deepStrictEqual(r.reopens, []);
  assert.match(r.comments[0], /closed by @maintainer1/);

  // Closed by a GitHub App bot (not github-actions): still an automated close,
  // so it reopens -- suffix match, not an allowlist.
  r = await run({ closer: "omnigent-ci[bot]" });
  assert.deepStrictEqual(r.reopens, [{ pull_number: 7, state: "open" }]);
  assert.match(r.comments[0], /Reopened/);

  // No `closed` event on record: nothing to preserve, so reopen.
  r = await run({ closer: null });
  assert.deepStrictEqual(r.reopens, [{ pull_number: 7, state: "open" }]);

  // Author closed it themselves: reopened (Read-only authors can't undo even
  // their own close).
  r = await run({ closer: "ext" });
  assert.deepStrictEqual(r.reopens, [{ pull_number: 7, state: "open" }]);
  assert.match(r.comments[0], /Reopened/);

  // Head branch deleted: explains instead of failing.
  r = await run({ branchExists: false });
  assert.deepStrictEqual(r.reopens, []);
  assert.match(r.comments[0], /no longer exists/);

  // Whole fork gone (head.repo null): same explanation.
  r = await run({ headRepo: null });
  assert.deepStrictEqual(r.reopens, []);
  assert.match(r.comments[0], /no longer exists/);

  // `/reopen` must be a command, not a mention: prose about it does nothing,
  // but a trailing newline or leading indent still counts.
  r = await run({ body: "see /reopened elsewhere" });
  assert.deepStrictEqual([r.reopens, r.comments], [[], []]);
  r = await run({ body: "  /reopen\n\nthanks!" });
  assert.deepStrictEqual(r.reopens, [{ pull_number: 7, state: "open" }]);

  // Already open, and merged: both silently ignored.
  r = await run({ state: "open" });
  assert.deepStrictEqual([r.reopens, r.comments], [[], []]);
  r = await run({ merged: true, state: "closed" });
  assert.deepStrictEqual([r.reopens, r.comments], [[], []]);

  console.log("reopen-pr.test.js: all assertions passed");
})();
