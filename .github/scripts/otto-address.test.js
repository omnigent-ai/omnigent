const assert = require("node:assert/strict");
const { test } = require("node:test");
const authorize = require("./otto-address.js");

async function run({ comment = {}, pull = {}, files = {}, apiFailure = false } = {}) {
  const outputs = {};
  const calls = [];
  await authorize({
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
      pulls: { get: async () => ({ data: {
        state: "open", draft: false, user: { login: "omni-resolve-agent[bot]", type: "Bot" },
        head: { repo: { full_name: "omnigent-ai/omnigent" }, ref: "resolve-agent/fix" },
        base: { ref: "main" }, ...pull,
      } }) },
      repos: { getContent: async ({ path, ref }) => {
        calls.push({ path, ref });
        if (apiFailure) throw new Error("API unavailable");
        return { data: { content: Buffer.from(files[path] ?? (
          path === ".github/MAINTAINER" ? "maintainer\n" : "# Feedback only\nReader\n"
        )).toString("base64") } };
      } },
    } },
  });
  return { outputs, calls };
}

test("allows an explicitly listed reader without consulting repository Write permissions", async () => {
  const { outputs, calls } = await run();
  assert.equal(outputs.authorized, "true");
  assert.equal(calls.length, 2);
  assert.ok(calls.every(call => call.ref === "main"));
});

test("maintainers do not need a duplicate allowlist entry", async () => {
  const { outputs } = await run({ comment: { user: { login: "maintainer", type: "User" } } });
  assert.equal(outputs.authorized, "true");
});

for (const comment of [
  { body: "/address anything" }, { body: "quoted /address" },
  { user: { login: "outsider", type: "User" } },
  { user: { login: "reader", type: "Bot" } },
  { issue_url: "https://api.github.com/repos/omnigent-ai/omnigent/issues/8" },
  { updated_at: "2026-11-06T02:00:00Z" },
]) {
  test(`rejects unsupported or unauthorized command ${JSON.stringify(comment)}`, async () => {
    assert.deepEqual((await run({ comment })).outputs, {});
  });
}

for (const pull of [
  { state: "closed" }, { draft: true },
  { user: { login: "human", type: "User" } },
  { head: { repo: { full_name: "other/omnigent" }, ref: "resolve-agent/fix" } },
  { head: { repo: { full_name: "omnigent-ai/omnigent" }, ref: "main" } },
]) {
  test(`rejects ineligible target ${JSON.stringify(pull)}`, async () => {
    assert.deepEqual((await run({ pull })).outputs, {});
  });
}

test("authorization fails closed when the trusted configuration cannot be read", async () => {
  await assert.rejects(run({ apiFailure: true }), /API unavailable/);
});
