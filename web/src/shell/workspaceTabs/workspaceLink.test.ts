import { describe, expect, it } from "vitest";

import { formatWorkspaceLink, parseWorkspaceLink } from "./workspaceLink";
import type { WorkspaceLink } from "./workspacePlace";

function parsed(text: string): WorkspaceLink {
  const result = parseWorkspaceLink(text);
  if (!result.ok) throw new Error(`expected ${text} to parse: ${result.reason}`);
  return result.link;
}

function refusal(text: string): string {
  const result = parseWorkspaceLink(text);
  if (result.ok) throw new Error(`expected ${text} to be refused`);
  return result.reason;
}

const PR = "https://github.com/o/r/pull/12";

describe("parseWorkspaceLink / formatWorkspaceLink", () => {
  const canonical: [string, WorkspaceLink][] = [
    ["omnigent://new", { place: { kind: "new" } }],
    ["omnigent://new?app=terminal", { place: { kind: "new", app: "terminal" } }],
    ["omnigent://files", { place: { kind: "files" } }],
    [
      "omnigent://files?path=src/a.ts&line=12-20&column=4&comment=c1",
      {
        place: {
          kind: "files",
          path: "src/a.ts",
          line: { start: 12, end: 20, column: 4 },
          comment: "c1",
        },
      },
    ],
    ["omnigent://files?path=/etc/hosts", { place: { kind: "files", path: "/etc/hosts" } }],
    ["omnigent://changes", { place: { kind: "changes" } }],
    [
      "omnigent://changes?path=a.go&line=40&base=main&head=HEAD",
      {
        place: {
          kind: "changes",
          path: "a.go",
          line: { start: 40 },
          range: { base: "main", head: "HEAD" },
        },
      },
    ],
    ["omnigent://changes?base=main", { place: { kind: "changes", range: { base: "main" } } }],
    [
      "omnigent://changes?path=a.go&base=main&file-tab=1",
      { place: { kind: "changes", path: "a.go", range: { base: "main" }, fileTab: true } },
    ],
    ["omnigent://pulls", { place: { kind: "pulls" } }],
    [
      `omnigent://pull?url=${encodeURIComponent(PR)}`,
      { place: { kind: "pull", url: PR, view: "summary" } },
    ],
    [
      `omnigent://pull?url=${encodeURIComponent(PR)}&view=changes&commit=abc1234&path=a.ts&line=3`,
      {
        place: {
          kind: "pull",
          url: PR,
          view: "changes",
          commit: "abc1234",
          path: "a.ts",
          line: { start: 3 },
        },
      },
    ],
    [
      `omnigent://pull?url=${encodeURIComponent(PR)}&focus=checks`,
      { place: { kind: "pull", url: PR, view: "summary", focus: "checks" } },
    ],
    ["omnigent://terminal/term%3A1", { place: { kind: "terminal", key: "term:1" } }],
    ["omnigent://agents", { place: { kind: "agents" } }],
    ["omnigent://tasks", { place: { kind: "tasks" } }],
    ["omnigent://agent/child-1", { place: { kind: "agent", id: "child-1" } }],
    ["omnigent://chat", { place: { kind: "chat" } }],
    ["omnigent://chat/conv-9", { place: { kind: "chat", id: "conv-9" } }],
    ["omnigent://search", { place: { kind: "search" } }],
    ["omnigent://search?q=hello%20world", { place: { kind: "search", query: "hello world" } }],
    ["omnigent://web", { place: { kind: "web" } }],
    ["https://example.com/a?b=1", { place: { kind: "web", url: "https://example.com/a?b=1" } }],
    [
      "omnigent://files?path=a.ts&session=s1&tab=t2",
      { place: { kind: "files", path: "a.ts" }, session: "s1", tab: "t2" },
    ],
    [
      `omnigent://web?url=${encodeURIComponent("https://example.com/?session=page")}&session=s1`,
      { place: { kind: "web", url: "https://example.com/?session=page" }, session: "s1" },
    ],
  ];

  it.each(canonical)("parses %s and formats it back", (text, link) => {
    expect(parsed(text)).toEqual(link);
    expect(formatWorkspaceLink(link)).toBe(text);
  });

  it("round-trips the largest safe line", () => {
    const text = "omnigent://files?path=a.ts&line=1-9007199254740991";
    expect(formatWorkspaceLink(parsed(text))).toBe(text);
  });

  it("round-trips paths with spaces, plus signs and unicode", () => {
    const link: WorkspaceLink = { place: { kind: "files", path: "docs/a b+c/ß.md" } };
    expect(parsed(formatWorkspaceLink(link))).toEqual(link);
  });

  it("canonicalizes paths, defaults and parameter order", () => {
    expect(parsed("omnigent://files?path=./src/../lib//b.ts/")).toEqual({
      place: { kind: "files", path: "lib/b.ts" },
    });
    expect(
      formatWorkspaceLink(parsed(`omnigent://pull?view=summary&url=${encodeURIComponent(PR)}`)),
    ).toBe(`omnigent://pull?url=${encodeURIComponent(PR)}`);
    expect(parsed("omnigent://files?path=a.ts&line=5-5")).toEqual({
      place: { kind: "files", path: "a.ts", line: { start: 5 } },
    });
    expect(parsed("OMNIGENT://Files")).toEqual({ place: { kind: "files" } });
    expect(parsed("omnigent://files#ignored")).toEqual({ place: { kind: "files" } });
  });

  it("drops and reports parameters a kind doesn't take", () => {
    const result = parseWorkspaceLink("omnigent://pulls?path=a.ts&zoom=2&tab=t1");
    expect(result).toEqual({
      ok: true,
      link: { place: { kind: "pulls" }, tab: "t1" },
      ignored: ["path", "zoom"],
    });
  });

  it("keeps a page's own session parameter out of the envelope", () => {
    expect(parsed("https://example.com/?session=page&tab=x")).toEqual({
      place: { kind: "web", url: "https://example.com/?session=page&tab=x" },
    });
  });

  it.each([
    ["omnigent://files?path=a.ts&line=5-2", "ends before it starts"],
    ["omnigent://files?path=a.ts&line=0", "isn't a valid line"],
    ["omnigent://files?path=a.ts&line=9007199254740992", "isn't a valid line"],
    [`omnigent://files?path=a.ts&line=${"9".repeat(400)}`, "isn't a valid line"],
    ["omnigent://files?path=a.ts&line=Infinity", "isn't a valid line"],
    ["omnigent://files?path=a.ts&line=1e3", "isn't a valid line"],
    ["omnigent://files?path=a.ts&line=3&column=9007199254740992", "isn't a valid column"],
    ["omnigent://files?path=a.ts&column=3", "A column needs a line"],
    ["omnigent://files?path=../secret", "goes above its folder"],
    ["omnigent://files?path=/../etc", "goes above its folder"],
    ["omnigent://files?path=.", "Name a file or folder"],
    ["omnigent://files?path=a%00b", "NUL"],
    ["omnigent://pull", "needs its url"],
    ["omnigent://pull?url=http%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F1", "must use https"],
    [`omnigent://pull?url=${encodeURIComponent(PR)}&view=checks`, "no view"],
    [`omnigent://pull?url=${encodeURIComponent(PR)}&commit=xyz`, "isn't a commit SHA"],
    ["omnigent://changes?head=HEAD", "needs a base"],
    ["omnigent://terminal", "needs an ID"],
    ["omnigent://agent", "needs an ID"],
    ["omnigent://files/abc", "doesn't take an ID"],
    ["omnigent://new?app=files", "no app"],
    ["omnigent://web?url=file%3A%2F%2F%2Fetc%2Fpasswd", "Only http and https"],
    ["omnigent://teleport", "no workspace place"],
    ["omnigent://agent/a/b", "at most one ID"],
    ["omnigent://terminal/%E0%A4%A", "percent-encoding"],
    ["ftp://example.com", "isn't a workspace link"],
    ["javascript:alert(1)", "isn't a workspace link"],
  ])("refuses %s", (text, reason) => {
    expect(refusal(text)).toContain(reason);
  });
});
