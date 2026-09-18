import { describe, expect, it } from "vitest";
import { describeMermaidError, escapeSequenceTextSemicolons } from "./MermaidError";

const PARSE_ERROR_LINE_2 =
  "Parse error on line 2:\n...once; twice\n-----^\nExpecting 'X', got 'NEWLINE'";

describe("escapeSequenceTextSemicolons", () => {
  it("escapes semicolons in message and note text after the colon", () => {
    const chart =
      "sequenceDiagram\n    A->>B: bind user; check policy\n    Note over A,B: once; twice\n";
    expect(escapeSequenceTextSemicolons(chart)).toEqual({
      text: "sequenceDiagram\n    A->>B: bind user#59; check policy\n    Note over A,B: once#59; twice\n",
      count: 2,
    });
  });

  it("escapes semicolons in block labels and participant aliases", () => {
    const chart =
      "sequenceDiagram\n    participant A as Agent; runtime\n    alt approved; logged\n        A->>B: go\n    end\n";
    expect(escapeSequenceTextSemicolons(chart)?.text).toBe(
      "sequenceDiagram\n    participant A as Agent#59; runtime\n    alt approved#59; logged\n        A->>B: go\n    end\n",
    );
  });

  it("splits at the first colon only, so colons inside the text survive", () => {
    expect(escapeSequenceTextSemicolons("sequenceDiagram\n    A->>B: at 10:30; go\n")?.text).toBe(
      "sequenceDiagram\n    A->>B: at 10:30#59; go\n",
    );
  });

  it("leaves semicolons outside free text alone and front matter untouched", () => {
    expect(escapeSequenceTextSemicolons("sequenceDiagram\n    A->>B: hi\n")).toBeNull();
    expect(
      escapeSequenceTextSemicolons("sequenceDiagram;\n    autonumber;\n    A->>B: hi\n"),
    ).toBeNull();
    const withFrontMatter =
      "---\ntitle: |\n  Note over A,B: once; twice\n---\nsequenceDiagram\n    Note over A,B: once; twice\n";
    expect(escapeSequenceTextSemicolons(withFrontMatter)).toEqual({
      text: "---\ntitle: |\n  Note over A,B: once; twice\n---\nsequenceDiagram\n    Note over A,B: once#59; twice\n",
      count: 1,
    });
  });
});

describe("describeMermaidError", () => {
  it("maps the reported line past front matter that quotes the diagram", () => {
    const chart =
      "---\ntitle: |\n  sequenceDiagram\n  Note over A,B: once; twice\n---\nsequenceDiagram\n  Note over A,B: once; twice\n";
    const details = describeMermaidError(chart, PARSE_ERROR_LINE_2);
    expect(details.line).toBe(7);
    expect(details.source).toBe("  Note over A,B: once; twice");
    expect(details.hint).not.toBeNull();
    expect(details.escaped?.count).toBe(1);
  });

  it("maps past directives and comment lines Mermaid strips", () => {
    const chart =
      '%%{init: {"theme": "dark"}}%%\n%% a comment\nsequenceDiagram\n  A->>B: once; twice\n';
    expect(describeMermaidError(chart, PARSE_ERROR_LINE_2).line).toBe(4);
  });

  it("offers neither the hint nor an escape outside sequence diagrams", () => {
    const details = describeMermaidError("flowchart LR\n  A --> B; C\n", PARSE_ERROR_LINE_2);
    expect(details.line).toBe(2);
    expect(details.hint).toBeNull();
    expect(details.escaped).toBeNull();
  });

  it("gives up gracefully when the error names no line", () => {
    expect(describeMermaidError("flowchart LR\n  A --> B", "Syntax error in text")).toEqual({
      line: null,
      source: null,
      hint: null,
      escaped: null,
    });
  });
});
