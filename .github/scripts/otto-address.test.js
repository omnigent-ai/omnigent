const assert = require("node:assert/strict");
const { test } = require("node:test");
const authorize = require("./otto-address.js");

async function run({ comment = {}, pull = {}, maintainers = "maintainer\n", apiFailure = false, reviewers = "Reader,second-reader\nthird-reader" } = {}) {
  const outputs = {};
  const calls = [];
  let pullReads = 0;
  await authorize({
    reviewers,
    context: {
      repo: { owner: "omnigent-ai", repo: "omnigent" },
      payload: { issue: { number: 7, pull_request: {} }, comment: { id: 42 } },
    },
    core: { info() {}, setOutput: (key, value) => { outputs[key] = value; } },
    github: { rest: {
      issues: { getComment: async () => ({ data: {
        body: "/address", user: { login: "reader", type: "User" },
        issue_url: "https://api.github.com/repos/omnigent-ai/omnigent/issues/7",
        created_at: "2026-11-06T01:00:00Z", updated_at: "2026-11-06T01:00:00Z",
        ...comment,
      } }) },
      pulls: { get: async () => {
        pullReads += 1;
        return { data: {
          state: "open", draft: false, user: { login: "omni-resolve-agent[bot]", type: "Bot" },
          head: { repo: { full_name: "omnigent-ai/omnigent" }, ref: "resolve-agent/fix" },
          ...pull,
        } };
      } },
      repos: { getContent: async ({ path, ref }) => {
        calls.push({ path, ref });
        if (apiFailure) throw new Error("API unavailable");
        assert.equal(path, ".github/MAINTAINER");
        return { data: { content: Buffer.from(maintainers).toString("base64") } };
      } },
    } },
  });
  return { outputs, calls, pullReads };
}

test("allows an explicitly listed reader without consulting repository Write permissions", async () => {
  const { outputs, calls } = await run();
  assert.equal(outputs.authorized, "true");
  assert.equal(calls.length, 1);
  assert.ok(calls.every(call => call.ref === "main"));
});

test("maintainers do not need a duplicate allowlist entry", async () => {
  const { outputs } = await run({ reviewers: "", comment: { user: { login: "maintainer", type: "User" } } });
  assert.equal(outputs.authorized, "true");
});

test("an unset variable does not authorize contributors", async () => {
  assert.deepEqual((await run({ reviewers: "" })).outputs, {});
});

test("accepts a command surrounded by whitespace", async () => {
  assert.equal((await run({ comment: { body: "\n /address \t\n" } })).outputs.authorized, "true");
});

for (const login of ["second-reader", "third-reader"]) {
  test(`accepts comma- and newline-separated usernames: ${login}`, async () => {
    assert.equal((await run({ comment: { user: { login, type: "User" } } })).outputs.authorized, "true");
  });
}

for (const comment of [
  { body: "/address anything" }, { body: "quoted /address" },
  { body: null },
  { user: { login: "reader", type: "Bot" } },
  { user: null },
  { issue_url: "https://api.github.com/repos/omnigent-ai/omnigent/issues/8" },
]) {
  test(`rejects unsupported or unauthorized command ${JSON.stringify(comment)}`, async () => {
    const { outputs, pullReads } = await run({ comment });
    assert.deepEqual(outputs, {});
    assert.equal(pullReads, 0);
  });
}

for (const pull of [
  { state: "closed" }, { draft: true },
  { user: { login: "human", type: "User" } },
  { user: null },
  { head: { repo: { full_name: "other/omnigent" }, ref: "resolve-agent/fix" } },
  { head: { repo: { full_name: "omnigent-ai/omnigent" }, ref: "main" } },
]) {
  test(`rejects ineligible target ${JSON.stringify(pull)}`, async () => {
    const { outputs } = await run({ pull });
    assert.equal(outputs.authorized, undefined);
    assert.match(outputs.reason, /Resolve-authored PR/);
  });
}

test("authorization fails closed when the trusted configuration cannot be read", async () => {
  await assert.rejects(run({ apiFailure: true }), /API unavailable/);
});

test("explains an authorized user's edited command without authorizing dispatch", async () => {
  const { outputs } = await run({ comment: { updated_at: "2026-11-06T02:00:00Z" } });
  assert.equal(outputs.authorized, undefined);
  assert.match(outputs.reason, /Edited commands/);
});

for (const scenario of [
  {},
  { pull: { state: "closed" } },
  { comment: { updated_at: "2026-11-06T02:00:00Z" } },
]) {
  test(`unlisted callers cannot cause public replies: ${JSON.stringify(scenario)}`, async () => {
    const { outputs, pullReads } = await run({
      ...scenario,
      comment: { ...scenario.comment, user: { login: "outsider", type: "User" } },
    });
    assert.deepEqual(outputs, {});
    assert.equal(pullReads, 0);
  });
}

for (const [login, permitted] of [["maintainer", true], ["second-maintainer", true], ["inline-comment", false], ["header", false]]) {
  test(`parses maintainer comments and CRLF: ${login}`, async () => {
    const { outputs } = await run({
      reviewers: "",
      maintainers: "# header\r\n\r\n  Maintainer\tSecond-Maintainer  # inline-comment\r\n",
      comment: { user: { login, type: "User" } },
    });
    assert.equal(outputs.authorized, permitted ? "true" : undefined);
  });
}
