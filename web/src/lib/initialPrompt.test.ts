import { describe, expect, it } from "vitest";

import { composeAskSubagentPrompt } from "./initialPrompt";

describe("composeAskSubagentPrompt", () => {
  it("quotes selected text + surrounding excerpt, then the question", () => {
    expect(
      composeAskSubagentPrompt(
        "Deletion vectors",
        "Deletion vectors mark rows as deleted without rewriting files.",
        "What are deletion vectors?",
      ),
    ).toBe(
      [
        "Selected from the source response:",
        "",
        "Selected text:",
        "",
        "> Deletion vectors",
        "",
        "Surrounding excerpt:",
        "",
        "> Deletion vectors mark rows as deleted without rewriting files.",
        "",
        "Question: What are deletion vectors?",
      ].join("\n"),
    );
  });

  it("omits the surrounding excerpt section when it is null", () => {
    expect(composeAskSubagentPrompt("Deletion vectors", null, "define it")).toBe(
      [
        "Selected from the source response:",
        "",
        "Selected text:",
        "",
        "> Deletion vectors",
        "",
        "Question: define it",
      ].join("\n"),
    );
  });

  it("prefixes every line of a multi-line excerpt with '> '", () => {
    const composed = composeAskSubagentPrompt("fn foo", "fn foo() {\n  bar()\n}", "explain");
    expect(composed).toContain("Surrounding excerpt:\n\n> fn foo() {\n>   bar()\n> }\n");
  });
});
