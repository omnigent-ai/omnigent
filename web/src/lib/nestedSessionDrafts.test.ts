import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "./types";
import {
  addNestedSessionDraft,
  nestedSessionSidebarKey,
  removeNestedSessionDraft,
  updateNestedSessionDraft,
  withNestedSessionDrafts,
  type NestedSessionDraft,
} from "./nestedSessionDrafts";
import { orderSessionFamilies } from "./sessionFamilies";

const source = {
  id: "parent",
  title: "Source task",
  createdAt: 1,
  labels: {},
  permissionLevel: null,
  projectId: "project",
} as Session;
const row = {
  id: "parent",
  title: "Source task",
  created_at: 1,
  updated_at: 5,
  labels: {},
  permission_level: null,
  project_id: "project",
  object: "conversation",
} as Conversation;

function drafts(client: QueryClient) {
  return client.getQueryData<readonly NestedSessionDraft[]>(["nested-session-drafts"])!;
}

describe("nested draft sidebar rows", () => {
  it("keeps a draft grouped, replaces it once across push/create races, and preserves its key", () => {
    const client = new QueryClient();
    addNestedSessionDraft(client, "temp:nested", source);
    const before = withNestedSessionDrafts([row], drafts(client));
    expect(orderSessionFamilies(before).map((item) => item.id)).toEqual(["parent", "temp:nested"]);
    expect(before[1]).toMatchObject({
      title: "New nested agent",
      provisional: true,
      nested_draft_source_id: "parent",
      project_id: "project",
      updated_at: 5,
    });
    expect(row.family).toBeUndefined();
    updateNestedSessionDraft(client, "temp:nested");
    expect(
      withNestedSessionDrafts([row], drafts(client))[1].nested_draft_source_id,
    ).toBeUndefined();
    expect(
      withNestedSessionDrafts(
        [row, { ...row, id: "announced", labels: { "omnigent.client_create_token": "nested" } }],
        drafts(client),
      ).map((item) => item.id),
    ).toEqual(["parent", "temp:nested"]);
    updateNestedSessionDraft(client, "temp:nested", "created");
    const pushed = { ...row, id: "created", title: "Implement tests", family: before[1].family };
    const rows = withNestedSessionDrafts(
      [row, { ...row, id: "temp:nested" }, pushed],
      drafts(client),
    );
    expect(rows.map((item) => item.id)).toEqual(["parent", "created"]);
    expect(rows[1]).toMatchObject({ title: "Implement tests", provisional: false });
    expect(nestedSessionSidebarKey("created")).toBe("temp:nested");
    removeNestedSessionDraft(client, "temp:nested");
    expect(withNestedSessionDrafts([row, pushed], drafts(client))).toEqual([row, pushed]);
    client.clear();
  });

  it("removes cancelled drafts and inherits existing family pins and project even off-page", () => {
    const client = new QueryClient();
    const family = { id: "family", representative_id: "parent", pinned: true, pinned_at: "50" };
    addNestedSessionDraft(client, "temp:other", { ...source, family });
    const rows = withNestedSessionDrafts([], drafts(client));
    expect(rows).toHaveLength(2);
    expect(rows[1]).toMatchObject({ family, project_id: "project" });
    removeNestedSessionDraft(client, "temp:other");
    expect(withNestedSessionDrafts([], drafts(client))).toEqual([]);
    client.clear();
  });
});
