"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  parseServerLabels,
  serverLabel,
  labeledWorkspace,
  withConnectLabel,
} = require("../src/server_labels");

const PICKED = "https://accounts.cloud.databricks.com/omnigent?o=123";
const WORKSPACE = "https://dbc-1234.cloud.databricks.com";
const OTHER = "https://dbc-5678.cloud.databricks.com";

describe("server labels", () => {
  it("keeps only origin → string entries from a settings value", () => {
    assert.deepEqual(parseServerLabels(undefined), {});
    assert.deepEqual(parseServerLabels(["x"]), {});
    assert.deepEqual(
      parseServerLabels({
        [WORKSPACE]: PICKED,
        "https://bad.example.com/path": PICKED, // not a bare origin
        "https://num.example.com": 42,
        "https://typo.example.com": "not a url", // hand-edited
      }),
      { [WORKSPACE]: PICKED },
    );
  });

  it("names any URL on a labeled host by its pick", () => {
    const labels = { [WORKSPACE]: PICKED };
    assert.equal(serverLabel(labels, `${WORKSPACE}/omnigent`), PICKED);
    assert.equal(serverLabel(labels, "https://other.example.com/"), null);
    assert.equal(serverLabel(labels, "not a url"), null);
    assert.equal(serverLabel(labels, 42), null);
    // Inherited keys never count as labels.
    assert.equal(serverLabel({}, "https://constructor"), null);
  });

  it("finds the workspace an account URL naming it reached before", () => {
    const labels = { [OTHER]: "https://accounts.cloud.databricks.com/?o=456", [WORKSPACE]: PICKED };
    // The same pick, whatever its path.
    assert.equal(labeledWorkspace(labels, PICKED), WORKSPACE);
    assert.equal(
      labeledWorkspace(labels, "https://accounts.cloud.databricks.com/?o=123"),
      WORKSPACE,
    );
    assert.equal(labeledWorkspace(labels, "https://accounts.cloud.databricks.com/?o=456"), OTHER);
    // No workspace named, another workspace, or another account host.
    assert.equal(labeledWorkspace(labels, "https://accounts.cloud.databricks.com/omnigent"), null);
    assert.equal(labeledWorkspace(labels, "https://accounts.cloud.databricks.com/?o=789"), null);
    assert.equal(labeledWorkspace(labels, "https://other.cloud.databricks.com/?o=123"), null);
    assert.equal(labeledWorkspace(labels, "not a url"), null);
  });

  it("labels a host sign-in moved to, and drops the label on a direct connect", () => {
    const recents = [`${WORKSPACE}/omnigent`, `${OTHER}/omnigent`];
    const moved = withConnectLabel({ [OTHER]: PICKED }, PICKED, `${WORKSPACE}/omnigent`, recents);
    assert.deepEqual(moved, { [OTHER]: PICKED, [WORKSPACE]: PICKED });
    assert.deepEqual(withConnectLabel(moved, WORKSPACE, `${WORKSPACE}/omnigent`, recents), {
      [OTHER]: PICKED,
    });
  });

  it("keeps labels only for hosts still in the recents", () => {
    const labels = { [OTHER]: PICKED };
    assert.deepEqual(withConnectLabel(labels, PICKED, `${WORKSPACE}/omnigent`, [WORKSPACE]), {
      [WORKSPACE]: PICKED,
    });
  });
});
