// Local unit test for reopen-notice.js -- mocks the GitHub client and runs the
// real decision logic. No network.

const assert = require("assert");
const path = require("path");
const script = require(path.resolve(".github/workflows/reopen-notice.js"));

// Run the script against a scenario; returns the comments it posted.
async function run({ author = "ext", closer = "maintainer1", merged = false, existing = [] }) {
  const comments = [];
  const github = {
    paginate: async () => existing.map((body) => ({ body })),
    rest: {
      issues: {
        listComments: "listComments",
        createComment: async ({ body }) => comments.push(body),
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: {
      pull_request: { number: 7, merged, user: { login: author } },
      sender: { login: closer },
    },
  };
  await script({ github, context, core: { info: () => {} } });
  return comments;
}

(async () => {
  // Maintainer closed a community PR: point the author at the maintainer, and
  // do NOT advertise /reopen (it would refuse them).
  let c = await run({});
  assert.strictEqual(c.length, 1);
  assert.match(c[0], /closed by a maintainer/);
  assert.doesNotMatch(c[0], /comment `\/reopen`/);

  // Author closed their own PR: advertise /reopen, since it works for them.
  c = await run({ closer: "ext" });
  assert.match(c[0], /`\/reopen`/);

  // Merged: not a close, say nothing.
  assert.deepStrictEqual(await run({ merged: true }), []);

  // Bot closer: it posts its own tailored notice, so stay quiet.
  assert.deepStrictEqual(await run({ closer: "github-actions[bot]" }), []);

  // Already notified (close -> reopen -> close): do not repeat.
  assert.deepStrictEqual(await run({ existing: ["<!-- reopen-notice -->\nClosed."] }), []);

  // An unrelated comment does not count as the notice.
  c = await run({ existing: ["lgtm"] });
  assert.strictEqual(c.length, 1);

  console.log("reopen-notice.test.js: all assertions passed");
})();
