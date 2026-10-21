// Offline fixture tests for the production closure handler. No GitHub writes.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const script = require("./reopen-notice.js");

const closedAt = "2026-10-02T12:00:00Z";
const otto = "omni-resolve-agent[bot]";
const human = (body, login = "maintainer1") => ({
  body, user: { login, type: "User" }, author_association: "MEMBER",
});

async function run({
  author = "ext", closer = "maintainer1", merged = false, existing = [],
  reviews = [], reviewComments = [], current = {}, events, failRead,
} = {}) {
  const comments = [];
  const reads = [];
  const pr = { number: 7, state: "closed", closed_at: closedAt, merged, user: { login: author } };
  const pages = {
    listComments: existing.map((entry) => typeof entry === "string" ? human(entry) : entry),
    listReviews: reviews,
    listReviewComments: reviewComments,
    listEventsForTimeline: events ?? [{
      event: "closed", created_at: closedAt, actor: { login: closer, type: "User" },
    }],
  };
  const github = {
    paginate: async (method, params) => {
      reads.push(method);
      assert.equal(params.per_page, 100);
      assert.equal(params.owner, "omnigent-ai");
      assert.equal(params.repo, "omnigent");
      assert.equal(params.issue_number ?? params.pull_number, 7);
      if (failRead === method) throw new Error("API unavailable");
      return pages[method];
    },
    rest: {
      issues: {
        listComments: "listComments", listEventsForTimeline: "listEventsForTimeline",
        createComment: async ({ body }) => comments.push(body),
      },
      pulls: {
        get: async () => ({ data: { ...pr, ...current } }),
        listReviews: "listReviews", listReviewComments: "listReviewComments",
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" }, actor: "workflow-rerunner",
    payload: { pull_request: pr, sender: { login: closer, type: "User" } },
  };
  await script({ github, context, core: { info: () => {} } });
  return { comments, reads };
}

const requested = (result) => {
  assert.equal(result.comments.length, 1);
  assert.match(result.comments[0], /@maintainer1, could you leave a one-line reason/);
  assert.doesNotMatch(result.comments[0], /@workflow-rerunner/);
};
const noRequest = (result) => {
  assert.equal(result.comments.length, 1);
  assert.doesNotMatch(result.comments[0], /one-line reason/);
};

test("community and self-closed PRs retain their existing notices", async () => {
  const result = await run();
  noRequest(result);
  assert.match(result.comments[0], /closed by a maintainer/);
  assert.doesNotMatch(result.comments[0], /comment `\/reopen`/);
  assert.deepEqual(result.reads, ["listComments"]);
  const self = await run({ closer: "ext" });
  noRequest(self);
  assert.match(self.comments[0], /comment `\/reopen`/);
});

test("only known Otto authors receive feedback requests", async () => {
  for (const author of [otto, "app/omni-resolve-agent", "omni-resolve-agent"]) {
    const result = await run({ author });
    requested(result);
    for (const category of ["wrong root cause", "design conflict", "wrong mechanism", "missed surface", "not needed", "other"]) {
      assert.ok(result.comments[0].includes(category));
    }
    assert.match(result.comments[0], /`\/reopen` only undoes automated closes/);
  }
  noRequest(await run({ author: "other[bot]" }));
});

test("merge, bot close and existing notices produce no new comments", async () => {
  for (const args of [
    { merged: true }, { closer: "github-actions[bot]" },
    { existing: ["<!-- reopen-notice -->\nClosed."] },
  ]) {
    assert.deepEqual((await run({ author: otto, ...args })).comments, []);
  }
});

test("all feedback categories and free-form reasons suppress requests", async () => {
  for (const body of [
    "wrong root cause", "conflicts with design", "design conflict", "wrong mechanism",
    "missed surface", "not needed", "other: fixed elsewhere", "This breaks retries.",
    "不是这里的问题", "See https://example.test/design/42", "Superseded by #42",
  ]) {
    noRequest(await run({ author: otto, existing: [body] }));
  }
});

test("explanations from another maintainer, reviews and inline comments count", async () => {
  for (const source of ["existing", "reviews", "reviewComments"]) {
    noRequest(await run({ author: otto, [source]: [human("This behavior is deliberate.", "other-maintainer")] }));
  }
  // The actual closer may have a non-member association (e.g. outside collaborator).
  noRequest(await run({ author: otto, existing: [{ ...human("Wrong diagnosis"), author_association: "NONE" }] }));
});

test("status, approval, quoted or hidden text and outsider guesses are not reasons", async () => {
  for (const body of ["", "lgtm", "LGTM!", "Thanks!", "Closed.", "Closing", "👍", "/reopen", "<!-- wrong root cause -->", "```\nnot needed\n```", "> wrong mechanism"]) {
    requested(await run({ author: otto, existing: [body] }));
  }
  requested(await run({ author: otto, existing: [{ ...human("not needed", "visitor"), author_association: "NONE" }] }));
});

test("explicit automated closure explanation counts but routine bot progress does not", async () => {
  const bot = (body) => ({ body, user: { login: otto, type: "Bot" } });
  noRequest(await run({ author: otto, existing: [bot("Replacement #42 merged. Closing this PR.")] }));
  for (const body of ["Review passed; ready to merge.", "Closing.", "This fixes duplicate callbacks."]) {
    requested(await run({ author: otto, existing: [bot(body)] }));
  }
});

test("timeline actor is tagged instead of sender, rerunner or author", async () => {
  requested(await run({ author: otto, closer: "event-sender", events: [
    { event: "closed", created_at: "2026-10-01T12:00:00Z", actor: { login: "old-closer" } },
    { event: "reopened" },
    { event: "closed", created_at: closedAt, actor: { login: "maintainer1", type: "User" } },
  ] }));
});

test("stale closures or unavailable/nonhuman timeline actors do not post", async () => {
  for (const args of [
    { current: { state: "open" } }, { current: { merged: true } },
    { current: { closed_at: "2026-10-03T12:00:00Z" } }, { events: [] },
    { events: [{ event: "closed", created_at: closedAt, actor: null }] },
    { events: [{ event: "closed", created_at: closedAt, actor: { login: "automation", type: "Bot" } }] },
    { events: [{ event: "closed", created_at: "2026-10-01T12:00:00Z", actor: { login: "previous" } }] },
  ]) {
    assert.deepEqual((await run({ author: otto, ...args })).comments, []);
  }
});

test("feedback and reason-only notices both remain idempotent on redelivery/reclose", async () => {
  for (const existing of [[], ["not needed"]]) {
    const first = await run({ author: otto, existing });
    assert.equal(first.comments.length, 1);
    for (const closer of ["maintainer1", "another-maintainer"]) {
      const retry = await run({ author: otto, closer, existing: [...existing, ...first.comments] });
      assert.deepEqual(retry.comments, []);
    }
  }
});

test("failed reads reject before any write", async () => {
  for (const failRead of ["listComments", "listReviews", "listReviewComments", "listEventsForTimeline"]) {
    await assert.rejects(run({ author: otto, failRead }), /API unavailable/);
  }
});

test("hidden spans cannot manufacture an automated closure explanation", async () => {
  for (const body of [
    "not nee<!-- machine metadata -->ded",
    "repla```machine output```cement #42",
    "repla~~~machine output~~~cement #42",
  ]) {
    requested(await run({
      author: otto,
      existing: [{ body, user: { login: otto, type: "Bot" } }],
    }));
  }
});
