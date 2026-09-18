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
      firstLine: 2,
    });
  });

  it("escapes semicolons in block labels and participant aliases", () => {
    const chart =
      "sequenceDiagram\n    participant A as Agent; runtime\n    alt approved; logged\n        A->>B: go\n    end\n";
    expect(escapeSequenceTextSemicolons(chart)?.text).toBe(
      "sequenceDiagram\n    participant A as Agent#59; runtime\n    alt approved#59; logged\n        A->>B: go\n    end\n",
    );
  });

  it("preserves existing entity codes and counts only the bare semicolons", () => {
    expect(escapeSequenceTextSemicolons("sequenceDiagram\n    A->>B: a#59; b; c\n")).toEqual({
      text: "sequenceDiagram\n    A->>B: a#59; b#59; c\n",
      count: 1,
      firstLine: 2,
    });
    expect(
      escapeSequenceTextSemicolons("sequenceDiagram\n    Note over A: love #9829; you\n"),
    ).toBeNull();
  });

  it("keeps semicolons that separate statements and escapes only punctuation", () => {
    const chart =
      "sequenceDiagram\n  A->>B: first; B->>C: second\n  C->>D: punctuation; retry me\n";
    expect(escapeSequenceTextSemicolons(chart)).toEqual({
      text: "sequenceDiagram\n  A->>B: first; B->>C: second\n  C->>D: punctuation#59; retry me\n",
      count: 1,
      firstLine: 3,
    });
    expect(
      escapeSequenceTextSemicolons("sequenceDiagram\n  A->>B: hi; there; B->>C: yo\n")?.text,
    ).toBe("sequenceDiagram\n  A->>B: hi#59; there; B->>C: yo\n");
  });

  it("declines a semicolon whose continuation could be a statement", () => {
    expect(
      escapeSequenceTextSemicolons("sequenceDiagram\n  A->>B: retry; end of story\n"),
    ).toBeNull();
    expect(escapeSequenceTextSemicolons("sequenceDiagram\n  A->>B: go; activate B\n")).toBeNull();
  });

  it("skips comment lines so they neither count nor set the first line", () => {
    expect(
      escapeSequenceTextSemicolons("%% A->>B: a; b\nsequenceDiagram\n  A->>B: c; d\n"),
    ).toEqual({
      text: "%% A->>B: a; b\nsequenceDiagram\n  A->>B: c#59; d\n",
      count: 1,
      firstLine: 3,
    });
  });

  it("normalizes CRLF line endings before matching", () => {
    expect(escapeSequenceTextSemicolons("sequenceDiagram\r\n    A->>B: a; b\r\n")).toEqual({
      text: "sequenceDiagram\n    A->>B: a#59; b\n",
      count: 1,
      firstLine: 2,
    });
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
      firstLine: 6,
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
    expect(details.escaped).toMatchObject({ count: 1, firstLine: 7 });
  });

  it("maps past directives and comment lines Mermaid strips", () => {
    const chart =
      '%%{init: {"theme": "dark"}}%%\n%% a comment\nsequenceDiagram\n  A->>B: once; twice\n';
    expect(describeMermaidError(chart, PARSE_ERROR_LINE_2).line).toBe(4);
  });

  it("offers the hint only for a bare semicolon, not an entity code", () => {
    const chart = "sequenceDiagram\n  A->>B: a#59; b\n  A=>B: again\n";
    const details = describeMermaidError(chart, PARSE_ERROR_LINE_2);
    expect(details.hint).toBeNull();
    expect(details.escaped).toBeNull();
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
