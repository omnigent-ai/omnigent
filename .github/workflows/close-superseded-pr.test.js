const path = require("path");
const script = require(
  path.resolve(".github/workflows/close-superseded-pr.js"),
);

async function run({
  merged = true,
  author = "omni-resolve-agent[bot]",
  body = "Supersedes #10",
  oldState = "open",
} = {}) {
  const comments = [];
  const closed = [];
  const github = {
    rest: {
      pulls: {
        get: async () => ({ data: { state: oldState } }),
        update: async ({ pull_number, state }) =>
          closed.push({ pull_number, state }),
      },
      issues: {
        createComment: async ({ issue_number, body: comment }) =>
          comments.push({ issue_number, body: comment }),
      },
    },
  };
  const context = {
    repo: { owner: "omnigent-ai", repo: "omnigent" },
    payload: {
      pull_request: {
        number: 20,
        merged,
        body,
        user: { login: author },
      },
    },
  };
  await script({ context, github });
  return { comments, closed };
}

function assert(name, condition, detail) {
  console.log(
    `${condition ? "PASS" : "FAIL"}  ${name}${detail ? ` -- ${detail}` : ""}`,
  );
  if (!condition) process.exitCode = 1;
}

(async () => {
  let result = await run();
  assert(
    "closes contributor PR only after trusted replacement merges",
    result.closed.length === 1 &&
      result.closed[0].pull_number === 10 &&
      result.comments.length === 1,
    JSON.stringify(result),
  );

  for (const values of [
    { merged: false },
    { author: "human-contributor" },
    { body: "Builds on #10" },
    { oldState: "closed" },
  ]) {
    result = await run(values);
    assert(
      `does nothing for ${JSON.stringify(values)}`,
      result.closed.length === 0 && result.comments.length === 0,
      JSON.stringify(result),
    );
  }
})();
