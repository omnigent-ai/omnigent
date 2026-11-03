// Run the pinned github-script action itself against a local GitHub HTTP fixture.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { spawn } = require("node:child_process");
const { createServer } = require("node:http");
const { mkdtemp, readFile, writeFile, rm } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const path = require("node:path");
const event = require("./fixtures/reopen-notice/closed.json");
const action = process.env.GITHUB_SCRIPT_BUNDLE;
assert.ok(action, "Set GITHUB_SCRIPT_BUNDLE to the pinned actions/github-script dist/index.js");

async function scenario(t, options = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), "otto-closure-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const eventPath = path.join(directory, "event.json");
  await writeFile(eventPath, JSON.stringify(event));
  const posts = [];
  const reads = [];
  let current = { ...event.pull_request };
  const bodyEntry = (body) => ({ body, user: event.sender, author_association: "MEMBER" });
  const entries = options.reason ? [bodyEntry(options.reason)] : [];
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, `http://${request.headers.host}`);
    response.setHeader("Content-Type", "application/json");
    assert.equal(request.headers.authorization, "token fixture-only-token");
    if (request.method === "POST") {
      assert.equal(url.pathname, "/repos/omnigent-ai/omnigent/issues/7/comments");
      let body = "";
      for await (const chunk of request) body += chunk;
      const comment = JSON.parse(body);
      posts.push(comment.body);
      entries.push(bodyEntry(comment.body));
      // Simulate a write accepted by GitHub whose response failed in transit.
      response.writeHead(options.lostResponse ? 500 : 201);
      response.end(JSON.stringify({ id: posts.length, ...comment }));
      return;
    }
    assert.equal(request.method, "GET");
    reads.push(url.pathname + url.search);
    if (options.failReviews && url.pathname.endsWith("/reviews")) {
      response.writeHead(403);
      response.end(JSON.stringify({ message: "fixture review access denied" }));
      return;
    }
    if (url.pathname === "/repos/omnigent-ai/omnigent/pulls/7") {
      response.end(JSON.stringify(current));
      return;
    }
    const base = "/repos/omnigent-ai/omnigent";
    const surfaces = {
      [`${base}/issues/7/comments`]: options.source === "reviews" || options.source === "inline" ? entries.filter((e) => e.body.includes("<!-- reopen-notice -->")) : entries,
      [`${base}/issues/7/timeline`]: [{ event: "closed", actor: event.sender, created_at: event.pull_request.closed_at }],
      [`${base}/pulls/7/reviews`]: options.source === "reviews" ? entries : [],
      [`${base}/pulls/7/comments`]: options.source === "inline" ? entries : [],
    };
    assert.ok(Object.hasOwn(surfaces, url.pathname), `Unexpected endpoint ${url.pathname}`);
    // Every surface has an empty first page; evidence and markers are on page 2.
    if (url.searchParams.get("page") !== "2") {
      url.searchParams.set("page", "2");
      response.setHeader("Link", `<${url.href}>; rel="next"`);
      response.end("[]");
    } else {
      if (options.reopenDuringRead && url.pathname.endsWith("/pulls/7/comments")) {
        current = { ...current, state: "open" };
      }
      response.end(JSON.stringify(surfaces[url.pathname]));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  const workflow = await readFile(path.join(__dirname, "reopen-notice.yml"), "utf8");
  const testWorkflow = await readFile(path.join(__dirname, "reopen-notice-test.yml"), "utf8");
  const pin = workflow.match(/uses: actions\/github-script@([a-f0-9]{40})/)[1];
  assert.ok(testWorkflow.includes(`ref: ${pin}`), "test and production action pins must agree");
  assert.match(workflow, /group: reopen-notice-\$\{\{ github.event.pull_request.number \}\}/);
  assert.match(workflow, /cancel-in-progress: false/);
  const wrapper = workflow.split("          script: |\n")[1]
    .trimEnd().split("\n").map((line) => line.slice(12)).join("\n");
  assert.match(wrapper, /await script\(\{ github, context, core \}\)/);
  async function invoke() {
    const child = spawn(process.execPath, [path.resolve(action)], {
      cwd: path.resolve(__dirname, "../.."),
      // Do not pass real credentials/provider endpoints into the action process.
      env: {
        PATH: process.env.PATH,
        GITHUB_EVENT_PATH: eventPath, GITHUB_EVENT_NAME: "pull_request_target",
        GITHUB_REPOSITORY: "omnigent-ai/omnigent", GITHUB_ACTOR: "workflow-rerunner",
        GITHUB_API_URL: endpoint, GITHUB_WORKSPACE: path.resolve(__dirname, "../.."),
        INPUT_DEBUG: "false", "INPUT_RESULT-ENCODING": "json",
        INPUT_SCRIPT: wrapper, "INPUT_GITHUB-TOKEN": "fixture-only-token",
        INPUT_RETRIES: "3", "INPUT_RETRY-EXEMPT-STATUS-CODES": "400,401,403,404,422",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.on("data", (chunk) => output += chunk);
    child.stderr.on("data", (chunk) => output += chunk);
    const timer = setTimeout(() => child.kill("SIGKILL"), 15000);
    const code = await new Promise((resolve, reject) => {
      child.on("error", reject);
      child.on("close", resolve);
    }).finally(() => clearTimeout(timer));
    return { code, output };
  }
  return { invoke, posts, reads };
}

test("serialized webhook -> pinned action/Octokit -> one paginated closure request", async (t) => {
  const fixture = await scenario(t);
  let result = await fixture.invoke();
  assert.equal(result.code, 0, result.output);
  assert.equal(fixture.posts.length, 1);
  assert.match(fixture.posts[0], /@maintainer1, could you leave a one-line reason/);
  assert.doesNotMatch(fixture.posts[0], /@workflow-rerunner/);
  for (const suffix of ["/issues/7/comments", "/issues/7/timeline", "/pulls/7/reviews", "/pulls/7/comments"]) {
    assert.ok(fixture.reads.some((url) => url.includes(`${suffix}?`) && url.includes("page=2")));
  }
  result = await fixture.invoke();
  assert.equal(result.code, 0, result.output);
  assert.equal(fixture.posts.length, 1, "redelivery must find the page-2 marker");
});

for (const source of ["comments", "reviews", "inline"]) {
  test(`a reason on page 2 of ${source} keeps the notice without reasking`, async (t) => {
    const fixture = await scenario(t, { source, reason: "This violates the retry design." });
    const result = await fixture.invoke();
    assert.equal(result.code, 0, result.output);
    assert.equal(fixture.posts.length, 1);
    assert.doesNotMatch(fixture.posts[0], /one-line reason/);
    assert.match(fixture.posts[0], /`\/reopen` only undoes automated closes/);
    await fixture.invoke();
    assert.equal(fixture.posts.length, 1);
  });
}

test("unavailable reviews fail with zero comments", async (t) => {
  const fixture = await scenario(t, { failReviews: true });
  const result = await fixture.invoke();
  assert.notEqual(result.code, 0);
  assert.match(result.output, /fixture review access denied/);
  assert.equal(fixture.posts.length, 0);
});

test("accepted comment with failed response is not retried; redelivery finds its marker", async (t) => {
  const fixture = await scenario(t, { lostResponse: true });
  const result = await fixture.invoke();
  assert.notEqual(result.code, 0);
  assert.equal(fixture.posts.length, 1);
  const retry = await fixture.invoke();
  assert.equal(retry.code, 0, retry.output);
  assert.equal(fixture.posts.length, 1);
});

test("reopening during evidence reads suppresses a stale request", async (t) => {
  const fixture = await scenario(t, { reopenDuringRead: true });
  const result = await fixture.invoke();
  assert.equal(result.code, 0, result.output);
  assert.equal(fixture.posts.length, 0);
});
