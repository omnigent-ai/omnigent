// The HTML preview's inline anchor script runs under the server's opt-in
// Content-Security-Policy only because the policy allows its hash.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { HTML_PREVIEW_HEAD } from "./codeViewerHelpers";

// Tests run from web/, as embed.test.tsx's relative read relies on too.
const POLICY_SOURCE = "../omnigent/server/data_controls.py";

describe("HTML preview script under the server's policy", () => {
  it("is allowed by a hash the server's policy lists", () => {
    const opening = "<script>";
    const body = HTML_PREVIEW_HEAD.slice(
      HTML_PREVIEW_HEAD.indexOf(opening) + opening.length,
      HTML_PREVIEW_HEAD.lastIndexOf("</script>"),
    );
    const digest = createHash("sha256").update(body, "utf8").digest("base64");
    expect(readFileSync(POLICY_SOURCE, "utf8"), digest).toContain(`'sha256-${digest}'`);
  });
});
