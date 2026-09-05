import { describe, expect, it } from "vitest";

import {
  buildArchiveConversationFilters,
  parseArchiveDateRange,
  type ArchiveLibraryViewState,
} from "./ArchiveLibraryToolbar";

const view: ArchiveLibraryViewState = {
  searchQuery: "handoff",
  searchScope: "content",
  project: "Omnigent",
  hostId: "host-win",
  agentName: "codex",
  dateField: "active_at",
  dateRange: "20260901-20260904",
  sortField: "title",
  order: "asc",
};

describe("ArchiveLibraryToolbar date ranges", () => {
  it("parses YYYYMMDD ranges with an inclusive end date", () => {
    const range = parseArchiveDateRange("20260901-20260904");
    expect(range).not.toBeNull();
    expect(new Date((range?.after ?? 0) * 1000).getDate()).toBe(1);
    expect(new Date((range?.before ?? 0) * 1000).getDate()).toBe(5);
  });

  it.each(["20260931-20261001", "20260904-20260901"])("rejects invalid range %s", (value) =>
    expect(parseArchiveDateRange(value)).toBeNull(),
  );

  it("maps compact view state to the Server filter contract", () => {
    const filters = buildArchiveConversationFilters(view, "debounced handoff");
    expect(filters).toMatchObject({
      searchQuery: "debounced handoff",
      searchScope: "content",
      project: "Omnigent",
      hostId: "host-win",
      agentName: "codex",
      dateField: "active_at",
      dateRange: "20260901-20260904",
      sortField: "title",
      order: "asc",
    });
  });

  it("accepts a single calendar day", () => {
    const range = parseArchiveDateRange("20260902");
    expect(range).not.toBeNull();
    expect((range?.before ?? 0) - (range?.after ?? 0)).toBe(86_400);
  });
});
