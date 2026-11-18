import { describe, expect, it } from "vitest";

import { markdownPreviewText } from "./markdownPreviewText";

describe("markdownPreviewText", () => {
  it("reads a step's Inputs, What it does, Outputs and Details as one line of text", () => {
    const goal = [
      "**Inputs:** the pull request named in the run's request, and its CI checks.",
      "",
      "**What it does:**",
      "- Reads the logs of each failing check",
      "- Fixes the cause and pushes",
      "",
      "**Outputs:** the PR with passing checks.",
      "",
      "**Details:** list the checks with `gh pr checks <number> --json name,state`.",
    ].join("\n");
    expect(markdownPreviewText(goal)).toBe(
      "Inputs: the pull request named in the run's request, and its CI checks. What it does: " +
        "Reads the logs of each failing check · Fixes the cause and pushes · Outputs: the PR " +
        "with passing checks. Details: list the checks with gh pr checks <number> --json name,state.",
    );
  });

  it("joins a paragraph's lines with spaces, and separates blocks", () => {
    expect(
      markdownPreviewText(
        [
          "Reads the logs of each",
          "failing check",
          "",
          "- Fixes the cause",
          "  and pushes",
          "- Reports back",
          "",
          "> Quoted on",
          "> two lines",
          "## Next",
          "Then stop",
        ].join("\n"),
      ),
    ).toBe(
      "Reads the logs of each failing check · Fixes the cause and pushes · Reports back · " +
        "Quoted on two lines · Next · Then stop",
    );
  });

  it("keeps code as written, without its backticks or fences", () => {
    expect(markdownPreviewText("Match `**/*.ts` and `` a`b `` in **`src`** files")).toBe(
      "Match **/*.ts and a`b in src files",
    );
    expect(markdownPreviewText("Run:\n\n```sh\npnpm test --filter web\n```\n\nThen push.")).toBe(
      "Run: pnpm test --filter web · Then push.",
    );
    // An escaped backtick is a backtick, not code.
    expect(markdownPreviewText("Type \\`not code\\` here")).toBe("Type `not code` here");
    // Code, like emphasis, can go on across a paragraph's lines.
    expect(markdownPreviewText("Keep `a\n*b*` as code, **all\nof it**")).toBe(
      "Keep a *b* as code, all of it",
    );
  });

  it("shows a link's label and an image's alt text, without block markers", () => {
    expect(
      markdownPreviewText(
        [
          "## Inputs",
          "> See [the runbook](https://example.com/runbook_(v2)) first.",
          "1. [x] Read ![the chart](chart.png)",
          "2) Open <https://example.com>",
        ].join("\n"),
      ),
    ).toBe("Inputs · See the runbook first. Read the chart · Open https://example.com");
  });

  it("keeps a table's cells and leaves out its rules", () => {
    expect(
      markdownPreviewText("| Flag | Use |\n| --- | :-: |\n| `--json` | Output |\n\n---\n\nDone."),
    ).toBe("Flag · Use · --json · Output · Done.");
    // An escaped pipe stays in its cell, in code too.
    expect(markdownPreviewText("| `a\\|b` | c\\|d |")).toBe("a|b · c|d");
  });

  it("leaves asterisks and underscores that mark nothing, and escaped ones, as written", () => {
    expect(
      markdownPreviewText(
        "Multiply 2 * 3 in my_file_name.py, not \\*this\\*, but _this_, *that* and ~~those~~.",
      ),
    ).toBe("Multiply 2 * 3 in my_file_name.py, not *this*, but this, that and those.");
    expect(markdownPreviewText("**a**, **b** _c_ `d` `e`")).toBe("a, b c d e");
  });

  it("keeps placeholders such as <date> as written", () => {
    expect(markdownPreviewText("Create the release-<date> branch from <base>.")).toBe(
      "Create the release-<date> branch from <base>.",
    );
  });

  it("drops private-use characters that would read as its code span placeholders", () => {
    const privateUse = String.fromCharCode(0xe000);
    expect(markdownPreviewText(`${privateUse}1${privateUse} and \`x\``)).toBe("1 and x");
  });

  it("reads only the start of a long source", () => {
    const preview = markdownPreviewText(`${"word ".repeat(400)}\n\n**Details:** sentinel`);
    expect(preview.startsWith("word word")).toBe(true);
    expect(preview).not.toContain("sentinel");
  });

  it("is empty when no text remains", () => {
    expect(markdownPreviewText("")).toBe("");
    expect(markdownPreviewText("---\n\n  \n-\n")).toBe("");
  });
});
