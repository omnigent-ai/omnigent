import { describe, expect, it } from "vitest";

import {
  compareItemMatches,
  itemQueryTokens as tokenizeQuery,
  matchItem,
  MatchTier,
} from "./itemRanking";
import type { QuickPickItem } from "./model";

function item(label: string, keywords?: string[], description?: string): QuickPickItem<string> {
  return { id: label, label, keywords, description, value: label };
}

const tierOf = (query: string, candidate: QuickPickItem<string>) =>
  matchItem(tokenizeQuery(query), candidate, "words")?.tier;

describe("matchItem", () => {
  it.each([
    ["new tab", item("New tab"), MatchTier.exactLabel],
    ["select theme", item("Select theme…"), MatchTier.exactLabel],
    ["select theme…", item("Select theme…"), MatchTier.exactLabel],
    ["archive a session...", item("Archive a Session…"), MatchTier.exactLabel],
    ["theme", item("Select theme…"), MatchTier.labelWordPrefix],
    ["tab new", item("New tab"), MatchTier.labelWordPrefix],
    ["ings", item("Open settings"), MatchTier.labelSubstring],
    ["theme", item("Appearance", ["theme", "mode"]), MatchTier.keyword],
    ["path", item("Open", [], "a/path/here"), MatchTier.keyword],
    ["new theme", item("New tab", ["theme"]), MatchTier.keyword],
    ["nt", item("New tab"), MatchTier.fuzzy],
    ["gostng", item("Go to Settings"), MatchTier.fuzzy],
  ])("puts %j against %j in tier %i", (query, candidate, tier) => {
    expect(tierOf(query, candidate)).toBe(tier);
  });

  it("drops scattered fuzzy matches only with words matching", () => {
    const scattered = item("Show the comments column");
    expect(tierOf("theme", scattered)).toBeUndefined();
    expect(matchItem(tokenizeQuery("theme"), scattered)?.tier).toBe(MatchTier.fuzzy);
  });

  it("highlights a label typed with its ellipsis, leaving the ellipsis plain", () => {
    const match = matchItem(tokenizeQuery("select theme…"), item("Select theme…"))!;
    expect(match.candidate.ranges.label).toEqual([
      { start: 0, end: 6 },
      { start: 7, end: 12 },
    ]);
  });

  it("returns null when any query word matches nothing", () => {
    expect(matchItem(tokenizeQuery("new zebra"), item("New tab"))).toBeNull();
  });

  it("highlights a description match under its own key", () => {
    const match = matchItem(tokenizeQuery("path"), item("Open", [], "a/path/here"))!;
    expect(match.candidate.ranges).toEqual({ description: [{ start: 2, end: 6 }] });
  });

  it("orders by tier before match quality", () => {
    const tokens = tokenizeQuery("theme");
    const label = matchItem(tokens, item("Something with a long label and theme"))!;
    const keyword = matchItem(tokens, item("Appearance", ["theme"]))!;
    expect(compareItemMatches(label, keyword)).toBeLessThan(0);
  });

  it("orders within a tier by where and how much of the label matched", () => {
    const tokens = tokenizeQuery("theme");
    const select = matchItem(tokens, item("Select theme…"))!;
    const terminal = matchItem(tokens, item("Terminal theme"))!;
    expect(compareItemMatches(select, terminal)).toBeLessThan(0);
  });
});
