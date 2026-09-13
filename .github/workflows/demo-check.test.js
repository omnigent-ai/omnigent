const assert = require("node:assert/strict");
const test = require("node:test");

const { bodyNeedsDemo } = require("./demo-check.js");

function body(type, demo) {
  return `## Demo

${demo}

## Type of change

- [${type === "Bug fix" ? "x" : " "}] Bug fix
- [${type === "Feature" ? "x" : " "}] Feature
- [${type === "UI / frontend change" ? "x" : " "}] UI / frontend change`;
}

test("a backend bug may use non-visual evidence", () => {
  assert.equal(bodyNeedsDemo(body("Bug fix", "N/A — verified by the Test Plan.")), false);
});

test("a backend feature may use a written reproduction", () => {
  assert.equal(bodyNeedsDemo(body("Feature", "Run `pytest tests/server/test_api.py`.")), false);
});

test("a UI change without media needs a demo", () => {
  assert.equal(bodyNeedsDemo(body("UI / frontend change", "The layout looks better.")), true);
});

test("a checked evidence-format box is not itself a visual demo", () => {
  assert.equal(
    bodyNeedsDemo(body("UI / frontend change", "- [x] Visual demo attached below")),
    true
  );
});

test("a UI change with media satisfies the policy", () => {
  assert.equal(
    bodyNeedsDemo(
      body(
        "UI / frontend change",
        "![updated layout](https://github.com/omnigent-ai/omnigent/assets/1/layout.png)"
      )
    ),
    false
  );
});
