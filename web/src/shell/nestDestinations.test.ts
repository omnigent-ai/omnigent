import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { nestDestinations } from "./nestDestinations";

function row(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    owner: "viewer",
    family: {
      id: `f-${id}`,
      representative_id: id,
      representative_title: `Nest ${id}`,
      pinned: false,
    },
    ...overrides,
  };
}

const conversation = row("self", { family: undefined });
const pick = (args: Partial<Parameters<typeof nestDestinations>[0]>) =>
  nestDestinations({
    conversation,
    loaded: [],
    searchHits: [],
    search: "",
    viewerId: "viewer",
    ...args,
  }).map((destination) => destination.id);

describe("nestDestinations", () => {
  it("matches loaded nests by name and keeps search hits that matched a member", () => {
    const loaded = [
      row("a"),
      row("b", { family: { ...row("b").family!, representative_title: "Docs" } }),
    ];
    const hit = row("m", { title: "Member session" });
    expect(pick({ loaded, searchHits: [hit], search: "nest" })).toEqual(["a", "m"]);
  });

  it("offers each nest once, leaving out the session's own, archived and others' nests", () => {
    const own = row("own", { family: { ...row("x").family!, id: "mine" } });
    const nested = { ...conversation, family: { ...row("y").family!, id: "mine" } };
    const loaded = [row("a"), row("a2", { family: row("a").family }), own];
    const searchHits = [row("old", { archived: true }), row("theirs", { owner: "someone" })];
    expect(pick({ conversation: nested, loaded, searchHits })).toEqual(["a2"]);
  });
});
