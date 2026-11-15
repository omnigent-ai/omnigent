import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import {
  type CachedList,
  listReconcileTargets,
  projectNamesResolver,
} from "@/lib/listReconcileTargets";
import {
  ALL_MEMBERSHIP_FIELDS,
  type MembershipChange,
  type MembershipField,
  filtersFromConversationQueryKey,
  filtersFromProjectQueryKey,
} from "@/lib/sessionListCache";

function conv(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: null,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...overrides,
  };
}

function mainList(ids: string[], key: unknown[] = ["conversations", "", false, null, "mine"]) {
  return {
    queryHash: JSON.stringify(key),
    filters: filtersFromConversationQueryKey(key),
    ids: new Set(ids),
    needsRefetch: false,
  } satisfies CachedList;
}

function folder(name: string, ids: string[], archived = false) {
  const key = ["project-sessions", name, "updated_at", archived];
  return {
    queryHash: JSON.stringify(key),
    filters: filtersFromProjectQueryKey(key),
    folder: name,
    ids: new Set(ids),
    needsRefetch: false,
  } satisfies CachedList;
}

const projects = projectNamesResolver([
  { id: "proj_a", name: "Alpha" },
  { id: "proj_b", name: "Beta" },
  { id: null, name: "Legacy" },
]);

function targets(
  lists: CachedList[],
  changed: [Conversation, MembershipField[]][],
  newRows: Conversation[] = [],
) {
  const changes = new Map<string, MembershipChange>(
    changed.map(([row, fields]) => [row.id, { row, fields: new Set(fields) }]),
  );
  return listReconcileTargets({
    lists,
    changes,
    newRows: new Map(newRows.map((row) => [row.id, row])),
    projectNamesOf: projects,
    viewerId: "me",
  });
}

describe("projectNamesResolver", () => {
  it("dual-reads project_id and the legacy label, and flags unknown ids", () => {
    expect(projects(conv("a", { project_id: "proj_a" }))).toEqual(new Set(["Alpha"]));
    expect(projects(conv("a", { labels: { omni_project: "Legacy" } }))).toEqual(
      new Set(["Legacy"]),
    );
    expect(projects(conv("a"))).toEqual(new Set());
    expect(projects(conv("a", { project_id: "proj_new" }))).toBeNull();
  });
});

describe("listReconcileTargets", () => {
  const main = mainList(["s1", "s2"]);
  const alpha = folder("Alpha", ["s1"]);
  const beta = folder("Beta", ["s9"]);
  const gamma = folder("Legacy", []);

  it("refetches only the lists the patch flagged", () => {
    expect(targets([{ ...alpha, needsRefetch: true }, beta, main], [])).toEqual(
      new Set([alpha.queryHash]),
    );
  });

  it("refetches the folders on both sides of a move, and nothing else", () => {
    const moved = conv("s1", { project_id: "proj_b" });
    expect(targets([main, alpha, beta, gamma], [[moved, ["project"]]])).toEqual(
      new Set([alpha.queryHash, beta.queryHash]),
    );
  });

  it("refetches every folder when the destination project isn't cached yet", () => {
    const moved = conv("s1", { project_id: "proj_new" });
    expect(targets([main, alpha, beta, gamma], [[moved, ["project"]]])).toEqual(
      new Set([alpha.queryHash, beta.queryHash, gamma.queryHash]),
    );
  });

  it("ignores a title change unless a search list could match it", () => {
    const renamed = conv("s2", { title: "new name" });
    expect(targets([main, alpha], [[renamed, ["title"]]])).toEqual(new Set());
    const search = mainList([], ["conversations", "new", false]);
    expect(targets([main, alpha, search], [[renamed, ["title"]]])).toEqual(
      new Set([search.queryHash]),
    );
  });

  it("refetches the lists an unarchived session may return to", () => {
    const restored = conv("s5", { archived: false, project_id: "proj_a" });
    const archivedAlpha = folder("Alpha", ["s5"], true);
    expect(targets([main, alpha, beta, archivedAlpha], [[restored, ["archived"]]])).toEqual(
      new Set([main.queryHash, alpha.queryHash]),
    );
  });

  it("refetches the archived lists an archived session joins", () => {
    const archived = conv("s1", { archived: true, project_id: "proj_a" });
    const archivedAlpha = folder("Alpha", [], true);
    const archivedBeta = folder("Beta", [], true);
    const archivedScope = mainList([], ["conversations", "", false, null, "archived"]);
    expect(
      targets([archivedAlpha, archivedBeta, archivedScope], [[archived, ["archived"]]]),
    ).toEqual(new Set([archivedAlpha.queryHash, archivedScope.queryHash]));
  });

  it("checks a new session against each list's filters", () => {
    const filed = conv("new", { project_id: "proj_a" });
    // The Mine list already inserted it locally.
    const mine = mainList(["new", "s1"]);
    const shared = mainList([], ["conversations", "", false, null, "shared"]);
    expect(targets([mine, shared, alpha, beta], [], [filed])).toEqual(new Set([alpha.queryHash]));
    // Another user's session shared with the viewer belongs only in Shared.
    const sharedRow = conv("theirs", { owner: "them" });
    expect(targets([mainList(["s1"]), shared, alpha], [], [sharedRow])).toEqual(
      new Set([shared.queryHash]),
    );
  });

  it("skips side chats and sub-agent children", () => {
    const side = conv("side", { labels: { "omnigent.side_chat": "1" } });
    const child = conv("child", { parent_session_id: "s1" });
    expect(targets([mainList([]), alpha], [], [side, child])).toEqual(new Set());
    expect(
      targets([mainList([])], [[side, [...ALL_MEMBERSHIP_FIELDS] as MembershipField[]]]),
    ).toEqual(new Set());
  });

  it("treats rows as the viewer's own on a server without sign-in", () => {
    // No viewer id: ownership falls back to the access level, as the sidebar's scopes do.
    const changes = new Map<string, MembershipChange>();
    const row = conv("new", { owner: "local", permission_level: null, project_id: "proj_a" });
    const shared = mainList([], ["conversations", "", false, null, "shared"]);
    expect(
      listReconcileTargets({
        lists: [mainList([]), shared, alpha],
        changes,
        newRows: new Map([[row.id, row]]),
        projectNamesOf: projects,
        viewerId: null,
      }),
    ).toEqual(new Set([mainList([]).queryHash, alpha.queryHash]));
  });

  it("matches project-filtered lists by project_id alone", () => {
    const picker = mainList([], ["conversations", "", true, "Alpha"]);
    const other = mainList([], ["conversations", "", true, "Beta"]);
    const archived = conv("s7", { archived: true, project_id: "proj_a" });
    expect(targets([picker, other], [[archived, ["archived"]]])).toEqual(
      new Set([picker.queryHash]),
    );
  });
});
