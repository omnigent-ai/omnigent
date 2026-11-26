import { describe, expect, it, vi } from "vitest";

import {
  paletteShowMoreItems,
  type SessionSearchGroupPaging,
  type SessionSearchRows,
  sessionSearchItems,
} from "./paletteSessionItems";
import type { PaletteSession } from "./usePaletteSessionSearch";

function paging(overrides: Partial<SessionSearchGroupPaging> = {}): SessionSearchGroupPaging {
  return {
    hasNextPage: true,
    isFetchNextPageError: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    ...overrides,
  };
}

const ROW = { id: "conv_a", label: "A" } as PaletteSession;

describe("paletteShowMoreItems", () => {
  it("keeps Show more when every match on the page was already listed above, where asked", () => {
    const group = paging();
    expect(paletteShowMoreItems("content", group, [], "In messages")).toEqual([]);
    const [more] = paletteShowMoreItems("content", group, [], "In messages", {
      keepWithoutRows: true,
    });

    expect(more).toMatchObject({ id: "more:content", label: "Show more", group: "In messages" });
    expect(more?.value).toMatchObject({ kind: "more", lastRowId: undefined });
    more?.value.load();
    expect(group.fetchNextPage).toHaveBeenCalledOnce();
  });

  it("names the last row, so the list can keep its place after loading", () => {
    const [more] = paletteShowMoreItems("title", paging(), [ROW], "Sessions");
    expect(more?.value).toMatchObject({ lastRowId: "conv_a" });
  });

  it.each([
    ["the server has no more", { hasNextPage: false }],
    ["the next page failed", { isFetchNextPageError: true }],
  ])("offers nothing when %s", (_, state) => {
    expect(paletteShowMoreItems("content", paging(state), [ROW], "In messages")).toEqual([]);
  });

  it("shows loading, and can't be chosen, while the next page loads", () => {
    const [more] = paletteShowMoreItems(
      "content",
      paging({ isFetchingNextPage: true }),
      [ROW],
      "In messages",
    );
    expect(more).toMatchObject({ label: "Loading…", disabledReason: "Loading more sessions" });
  });
});

describe("sessionSearchItems", () => {
  const GROUPS = { names: "Sessions", messages: "In messages" };
  const HIGHLIGHT = { labelQuery: "a", snippetQuery: "a" };
  const MESSAGE = { id: "conv_m", label: "M" } as PaletteSession;
  const rows = (overrides: Partial<SessionSearchRows>): SessionSearchRows => ({
    visibleSessions: [ROW],
    contentSessions: [MESSAGE],
    showingServerMatches: true,
    titleSearch: paging(),
    contentSearch: paging(),
    ...overrides,
  });
  const ids = (built: ReturnType<typeof sessionSearchItems>) => ({
    names: built.names.map((item) => item.id),
    messages: built.messages.map((item) => item.id),
  });

  it("lists each group's rows, then its Show more", () => {
    expect(ids(sessionSearchItems(rows({}), GROUPS, HIGHLIGHT))).toEqual({
      names: ["conv_a", "more:title"],
      messages: ["conv_m", "more:content"],
    });
  });

  it("offers no page for a query the server hasn't answered yet", () => {
    const built = sessionSearchItems(
      rows({ showingServerMatches: false, contentSessions: [] }),
      GROUPS,
      HIGHLIGHT,
    );
    expect(ids(built)).toEqual({ names: ["conv_a"], messages: [] });
  });

  it("hides a message group whose page's matches are all listed by name, as ⌘K does", () => {
    const built = sessionSearchItems(rows({ contentSessions: [] }), GROUPS, HIGHLIGHT);
    expect(ids(built).messages).toEqual([]);
  });

  it("keeps that group's Show more where asked, so the later pages stay in reach", () => {
    const built = sessionSearchItems(rows({ contentSessions: [] }), GROUPS, HIGHLIGHT, {
      keepPagingWithoutRows: true,
    });
    expect(ids(built).messages).toEqual(["more:content"]);
    expect(built.messages[0]?.group).toBe("In messages");
  });
});
