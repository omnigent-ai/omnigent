import { describe, expect, it } from "vitest";

import { markdownListStackFormat } from "@/lib/pullRequestStacks/formats/markdownListStack";
import type { StackParse, StackSelf } from "@/lib/pullRequestStacks/stackModel";
import { EXAMPLE_STACK_FORMAT } from "@/test/pullRequestIntegrations";

const prUrl = (number: number) => `https://github.com/acme/app/pull/${number}`;
const SELF: StackSelf = { host: "github.com", repository: "acme/app", number: 102 };
const parse = (body: string) => EXAMPLE_STACK_FORMAT.parse(body, SELF);
const shape = (parsed: StackParse | null) =>
  parsed?.nodes.map((node) => [
    node.branch,
    node.ref?.number ?? null,
    node.depth,
    node.parentIndex,
  ]);
const tolerancesOf = (parsed: StackParse | null) =>
  parsed?.trust === "tolerated" ? parsed.tolerances : [];

const HEADER = "## Stacked changes (_written by stk_)";
const INTRO = `Review the changes since the PR below [here](${prUrl(102)}/files/a..b).`;
const ENTRIES = [
  `- [stack/base](${prUrl(100)}) [[Files changed](${prUrl(100)}/files)]`,
  `  - [stack/middle](${prUrl(101)}) [[Files changed](${prUrl(101)}/files/a..b)]`,
  `    - [**stack/current**](${prUrl(102)}) [[Files changed](${prUrl(102)}/files/b..c)] ← _this PR_`,
];
const described = (lines: string[], header = HEADER) =>
  [header, ...lines, "", "---------", "## Summary", "The change.", ""].join("\n");
const LINEAR = described([INTRO, ...ENTRIES]);

describe("markdownListStackFormat on its producer's output", () => {
  it("verifies a linear stack and spans exactly the header through the footer", () => {
    const parsed = parse(LINEAR);
    expect(parsed).toMatchObject({ format: "example-stack", trust: "verified", selfIndex: 2 });
    expect(shape(parsed)).toEqual([
      ["stack/base", 100, 0, null],
      ["stack/middle", 101, 1, 0],
      ["stack/current", 102, 2, 1],
    ]);
    if (parsed?.trust !== "verified") throw new Error("expected a verified parse");
    expect(LINEAR.slice(parsed.span.end)).toBe("## Summary\nThe change.\n");
  });

  it("starts its span at the header, leaving the text above it out", () => {
    const parsed = parse(`Context first.\n\n${LINEAR}`);
    if (parsed?.trust !== "verified") throw new Error("expected a verified parse");
    expect(`Context first.\n\n${LINEAR}`.slice(parsed.span.start, parsed.span.end)).toBe(
      LINEAR.slice(0, LINEAR.indexOf("## Summary")),
    );
  });

  it.each([
    ["a line separator", "\u2028"],
    ["a paragraph separator", "\u2029"],
  ])("verifies labels with %s, which git allows in a branch name", (_name, separator) => {
    const base = `stack/base${separator}split`;
    const current = `stack/current${separator}split`;
    const parsed = parse(
      LINEAR.replace("[stack/base]", `[${base}]`).replace(
        "[**stack/current**]",
        `[**${current}**]`,
      ),
    );
    expect(parsed?.trust).toBe("verified");
    expect(shape(parsed)).toEqual([
      [base, 100, 0, null],
      ["stack/middle", 101, 1, 0],
      [current, 102, 2, 1],
    ]);
  });

  it("verifies a branch named `@`, which git allows", () => {
    const parsed = parse(LINEAR.replace("[stack/middle]", "[@]"));
    expect(parsed?.trust).toBe("verified");
    expect(shape(parsed)?.[1]).toEqual(["@", 101, 1, 0]);
  });

  it("verifies without the header suffix or the intro line, which the syntax makes optional", () => {
    expect(parse(described(ENTRIES, "## Stacked changes"))?.trust).toBe("verified");
  });

  it("sets a leading dependency apart and lifts the stack under it to the root", () => {
    const parsed = parse(
      described([
        `- [stack/dependency](${prUrl(99)}) ⬆️ (dependency: merge first)`,
        `  - [**stack/current**](${prUrl(102)}) ← _this PR_`,
        `    - [stack/child](${prUrl(103)})`,
      ]),
    );
    expect(parsed?.trust).toBe("verified");
    expect(parsed?.dependency).toMatchObject({ branch: "stack/dependency", ref: { number: 99 } });
    expect(shape(parsed)).toEqual([
      ["stack/current", 102, 0, null],
      ["stack/child", 103, 1, 0],
    ]);
  });

  it("reads merged and unpushed entries, and leaves a stack of one unmarked", () => {
    const parsed = parse(
      described([
        `- [stack/base](${prUrl(100)}) [MERGED]`,
        "  - stack/unpushed",
        `    - [**stack/current**](${prUrl(102)}) ← _this PR_`,
      ]),
    );
    expect(parsed?.nodes[0]?.markers.merged).toBe(true);
    expect(parsed?.nodes[1]?.ref).toBeNull();
    expect(parse(described([`- [stack/only](${prUrl(102)})`]))).toMatchObject({
      trust: "verified",
      selfIndex: 0,
    });
  });

  it("carries the syntax's landing tool, and none for a syntax without one", () => {
    expect(EXAMPLE_STACK_FORMAT.landing?.landName).toBe("stk land");
    expect(markdownListStackFormat({ id: "plain", header: "## Plain" }).landing).toBeUndefined();
  });
});

describe("markdownListStackFormat on hand-edited descriptions", () => {
  it.each([
    ["a reworded header", LINEAR.replace(HEADER, "## Stacked changes, edited"), "header"],
    ["no footer", LINEAR.replace("\n---------\n", "\n"), "footer"],
    [
      "a line of prose",
      LINEAR.replace("\n\n---------", "\nSome prose.\n\n---------"),
      "unexpected-line",
    ],
    ["odd indentation", LINEAR.replace("  - [stack/middle]", "   - [stack/middle]"), "indentation"],
    ["the bold marker without the pointer", LINEAR.replace(" ← _this PR_", ""), "current-marker"],
    ...[
      "stack/middle.lock",
      "stack/middle/",
      "stack//middle",
      "stack/.middle",
      "stack/middle.",
      "HEAD",
      "-oops",
    ].map(
      (label) =>
        [
          `a label git can't create, ${label}`,
          LINEAR.replace("[stack/middle]", `[${label}]`),
          "unexpected-line",
        ] as [string, string, string],
    ),
  ])("tolerates %s for navigation only", (_case, body, tolerance) => {
    const parsed = parse(body);
    expect(parsed?.trust).toBe("tolerated");
    expect(tolerancesOf(parsed)).toContain(tolerance);
    expect(parsed?.nodes[parsed.selfIndex]?.ref?.number).toBe(102);
  });

  it("finds no stack without its header, under two headers, or in another syntax's header", () => {
    expect(parse(LINEAR.replace(`${HEADER}\n`, ""))).toBeNull();
    expect(parse(`${LINEAR}\n${LINEAR}`)).toBeNull();
    expect(parse(LINEAR.replace("## Stacked changes", "## Stack list"))).toBeNull();
  });

  it("matches its header as a whole word, so a longer heading is neither its section nor a second one", () => {
    const shorter = markdownListStackFormat({ id: "short-stack", header: "## Stack" });
    expect(shorter.parse(LINEAR, SELF)).toBeNull();
    expect(parse(`${LINEAR}\n## Stacked changesets\n\nThe release's own notes.\n`)?.trust).toBe(
      "verified",
    );
  });

  it("ends a section without a footer at its first blank line, so a list after it isn't read as entries", () => {
    const withoutFooter = LINEAR.replace("\n---------\n", "\n");
    const parsed = parse(
      withoutFooter.replace("\n## Summary", "\n- [x] tests pass\n- [ ] docs\n## Summary"),
    );
    expect(tolerancesOf(parsed)).toContain("footer");
    expect(shape(parsed)).toEqual(shape(parse(withoutFooter)));
  });

  it("ends the entries at a blank line even with a footer, so a list after it is never verified", () => {
    const personalList = parse(
      LINEAR.replace(
        "\n---------\n",
        "\n- [fix/related](https://github.com/acme/app/pull/90)\n---------\n",
      ),
    );
    expect(personalList?.trust).toBe("tolerated");
    expect(tolerancesOf(personalList)).toContain("unexpected-line");
    expect(personalList?.nodes.map((node) => node.branch)).not.toContain("fix/related");
    expect(shape(personalList)).toEqual(shape(parse(LINEAR)));
    expect(parse(LINEAR)?.trust).toBe("verified");
  });

  it("never verifies a section a code example holds", () => {
    // A no-break space can't close the fence, and an outdented fence opens a new one.
    for (const before of [
      "```\n```\u00a0\n",
      "- Example\n  ```\n  sample\n```\n",
      "- Example\n   ```\n   sample\n ```\n",
      "-\tExample\n   ```\n",
    ]) {
      expect(parse(`${before}${LINEAR}\`\`\`\n`)).toBeNull();
    }
  });

  it("never verifies a section inside an HTML comment", () => {
    expect(parse(`<!--\n${LINEAR}-->\n`)).toBeNull();
  });

  it("reads a section after a list item's code example", () => {
    expect(parse(`- Example\n  \`\`\`\n  sample\n${LINEAR}`)?.trust).toBe("verified");
  });

  it("reads the same description the same way every time, even with a global intro pattern", () => {
    const format = markdownListStackFormat({
      id: "global-intro",
      header: "## Stacked changes",
      headerSuffix: " (_written by stk_)",
      introLine: /^Review the changes/g,
    });
    const first = format.parse(LINEAR, SELF);
    expect(format.parse(LINEAR, SELF)).toEqual(first);
    expect(first).not.toBeNull();
  });

  it("ignores a footer inside a code block or quote above the header", () => {
    const withoutFooter = LINEAR.replace("\n---------\n", "\n");
    for (const above of ["```\n---------\n```", "> ---------"]) {
      const parsed = parse(`${above}\n\n${withoutFooter}`);
      expect(parsed?.trust).toBe("tolerated");
      expect(tolerancesOf(parsed)).toContain("footer");
    }
  });
});
