import { describe, expect, it } from "vitest";

import { detectPullRequestShorthand } from "./detect";

const found = (text: string) =>
  detectPullRequestShorthand(text).map(({ owner, repo, number, start, end }) => ({
    text: text.slice(start, end),
    owner,
    repo,
    number,
  }));

describe("detectPullRequestShorthand", () => {
  it("finds a number, a repository's number, and an owner's repository's number", () => {
    expect(found("Stacked on #13633, app#12, acme/app#7 and acme/site.js#8.")).toEqual([
      { text: "#13633", owner: null, repo: null, number: 13633 },
      { text: "app#12", owner: null, repo: "app", number: 12 },
      { text: "acme/app#7", owner: "acme", repo: "app", number: 7 },
      { text: "acme/site.js#8", owner: "acme", repo: "site.js", number: 8 },
    ]);
  });

  it("finds a managed user's repository, whose login holds an underscore", () => {
    expect(found("See octo-user_acme/app#2.")).toEqual([
      { text: "octo-user_acme/app#2", owner: "octo-user_acme", repo: "app", number: 2 },
    ]);
  });

  it("finds references at the edges of the text and inside punctuation", () => {
    expect(found("#1")).toEqual([{ text: "#1", owner: null, repo: null, number: 1 }]);
    expect(found("(#2)").map((reference) => reference.text)).toEqual(["#2"]);
    expect(found("see #3: done").map((reference) => reference.text)).toEqual(["#3"]);
  });

  it.each([
    ["a language name", "Written in C#7 and F#1."],
    ["a heading anchor", "Read file.md#3 first."],
    ["a URL fragment", "https://example.com/page#12"],
    ["a line reference", "#12:3"],
    ["an HTML entity", "&#39;"],
    ["a word", "issue#12abc"],
    ["a mention", "@team#4"],
    ["a leading zero", "#012"],
    ["a number too large", "#99999999999"],
    ["no hash", "PR 12"],
  ])("ignores %s", (_case, text) => {
    expect(found(text)).toEqual([]);
  });

  it("stops at a number beyond GitHub's range but keeps the ones within it", () => {
    expect(found("#2147483647 #2147483648").map((reference) => reference.number)).toEqual([
      2147483647,
    ]);
  });
});
