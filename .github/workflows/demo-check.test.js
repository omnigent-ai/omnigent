// Local unit test for demo-check.js -- mocks the GitHub client and runs the
// real decision logic. No network. The script runs two paginated searches
// (recently-opened PRs + PRs already labeled needs-demo), unions them, and per
// PR either comments+labels a new offender, or clears the label once an
// already-labeled PR satisfies the demo requirement.

const path = require("path");
const script = require(path.resolve(".github/workflows/demo-check.js"));

// A PR body that checks the given type box, optionally with a Demo section.
function body({ type = "Bug fix", demo = "" } = {}) {
  const checked = type ? `- [x] ${type}\n` : "- [ ] Bug fix\n";
  return `## Type of change\n${checked}\n## Demo\n${demo}\n`;
}

const IMAGE = "![screenshot](https://example.com/shot.png)";

// Build a PR node shaped like the GraphQL response.
function pr({ number, body: prBody = body(), author = "ext", assoc = "CONTRIBUTOR", isDraft = false, labels = [] }) {
  return {
    number,
    author: { login: author },
    authorAssociation: assoc,
    isDraft,
    labels: { nodes: labels.map((name) => ({ name })) },
    body: prBody,
  };
}

// Run the script. `recent` are PRs returned by the created:> search; `labeled`
// are PRs returned by the label:needs-demo search. Returns the side effects.
async function run({ recent = [], labeled = [], maintainerFile = "" } = {}) {
  const commented = [];
  const added = [];
  const removed = [];
  const warnings = [];
  const github = {
    // Route by search query: the label search contains "label:", the recent
    // search does not. Single page each.
    graphql: async (_query, vars) => {
      const nodes = vars.searchQuery.includes("label:") ? labeled : recent;
      return {
        rateLimit: { remaining: 4999, resetAt: "n/a" },
        search: { pageInfo: { hasNextPage: false, endCursor: null }, nodes },
      };
    },
    rest: {
      repos: {
        getContent: async () => ({
          data: { content: Buffer.from(maintainerFile, "utf8").toString("base64") },
        }),
      },
      issues: {
        createLabel: async () => {},
        addLabels: async ({ issue_number, labels }) => added.push({ issue_number, labels }),
        removeLabel: async ({ issue_number, name }) => removed.push({ issue_number, name }),
        createComment: async ({ issue_number, body }) => commented.push({ issue_number, body }),
      },
    },
  };
  const core = { warning: (m) => warnings.push(m) };
  const context = { repo: { owner: "omnigent-ai", repo: "omnigent" } };
  await script({ context, github, core });
  const nums = (arr) => arr.map((x) => x.issue_number).sort((a, b) => a - b);
  return { commented: nums(commented), added: nums(added), removed: nums(removed), warnings };
}

function eq(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

(async () => {
  // 1. New PR requiring a demo with none present -> commented + labeled.
  let r = await run({ recent: [pr({ number: 1, body: body({ demo: "N/A" }) })] });
  assert("new demo-less PR is commented and labeled",
    eq(r.commented, [1]) && eq(r.added, [1]) && eq(r.removed, []), JSON.stringify(r));

  // 2. New PR that already includes a demo -> left alone.
  r = await run({ recent: [pr({ number: 2, body: body({ demo: IMAGE }) })] });
  assert("new PR with a demo is not touched",
    eq(r.commented, []) && eq(r.added, []) && eq(r.removed, []), JSON.stringify(r));

  // 3. Labeled PR that gained a demo -> label cleared, no new comment. This is
  //    the core bug: previously the PR was skipped and kept the stale label.
  r = await run({ labeled: [pr({ number: 3, body: body({ demo: IMAGE }), labels: ["needs-demo"] })] });
  assert("labeled PR with a demo now has its label cleared",
    eq(r.removed, [3]) && eq(r.commented, []) && eq(r.added, []), JSON.stringify(r));

  // 4. Labeled PR that unchecked the type box (no longer requires a demo) ->
  //    label cleared.
  r = await run({ labeled: [pr({ number: 4, body: body({ type: "", demo: "N/A" }), labels: ["needs-demo"] })] });
  assert("labeled PR that no longer requires a demo has its label cleared",
    eq(r.removed, [4]) && eq(r.commented, []), JSON.stringify(r));

  // 5. Labeled PR that still lacks a demo -> nothing (no second comment, no
  //    removal). The no-duplicate-comment guarantee is preserved.
  r = await run({ labeled: [pr({ number: 5, body: body({ demo: "N/A" }), labels: ["needs-demo"] })] });
  assert("labeled PR still lacking a demo is left labeled, not re-commented",
    eq(r.removed, []) && eq(r.commented, []) && eq(r.added, []), JSON.stringify(r));

  // 6. A PR present in BOTH searches is processed once (deduped by number).
  const dupe = pr({ number: 6, body: body({ demo: IMAGE }), labels: ["needs-demo"] });
  r = await run({ recent: [dupe], labeled: [dupe] });
  assert("PR in both searches is deduped -> label removed exactly once",
    eq(r.removed, [6]) && r.removed.length === 1, JSON.stringify(r));

  // 7. Draft PR is skipped even when it would otherwise qualify.
  r = await run({ recent: [pr({ number: 7, body: body({ demo: "N/A" }), isDraft: true })] });
  assert("draft PR is skipped", eq(r.commented, []) && eq(r.added, []), JSON.stringify(r));

  // 8. Maintainer PR (by association) is skipped.
  r = await run({ recent: [pr({ number: 8, body: body({ demo: "N/A" }), assoc: "MEMBER" })] });
  assert("maintainer PR (association) is skipped", eq(r.commented, []) && eq(r.added, []), JSON.stringify(r));

  // 9. Maintainer PR (by MAINTAINER file) is skipped.
  r = await run({
    recent: [pr({ number: 9, body: body({ demo: "N/A" }), author: "boss" })],
    maintainerFile: "boss\n",
  });
  assert("maintainer PR (MAINTAINER file) is skipped", eq(r.commented, []) && eq(r.added, []), JSON.stringify(r));
})();
