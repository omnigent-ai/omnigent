import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

import { MoveToNestPicker } from "./MoveToNestPicker";

const rows = vi.hoisted(() => ({
  loaded: [] as Conversation[],
  searchHits: [] as Conversation[],
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  useSessionSearch: (query: string) => ({
    data: query ? { pages: [{ data: rows.searchHits }] } : undefined,
  }),
}));
vi.mock("@/hooks/useSidebarData", () => ({
  useOptionalLoadedConversations: () => ({ pages: [{ data: rows.loaded }] }),
}));
vi.mock("@/hooks/useDebouncedValue", () => ({ useDebouncedValue: <T,>(value: T) => value }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "viewer" }));

function session(id: string, title: string, nest?: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    owner: "viewer",
    ...(nest && {
      family: {
        id: `nest-${id}`,
        representative_id: id,
        representative_title: nest,
        pinned: false,
      },
    }),
  };
}

const SESSION = session("c1", "Fix the parser");

function renderPicker(onPick = vi.fn()) {
  const info = { ...FALLBACK_SERVER_INFO, sibling_sessions: true } as ServerInfo;
  render(
    <CapabilitiesProvider info={info}>
      <MoveToNestPicker conversation={SESSION} onPick={onPick} onClose={vi.fn()} />
    </CapabilitiesProvider>,
  );
  return { onPick, input: screen.getByRole("combobox", { name: "Move session to a nest" }) };
}

afterEach(() => {
  cleanup();
  rows.loaded = [];
  rows.searchHits = [];
});

describe("MoveToNestPicker", () => {
  it("keeps a search hit on a member's title, offered under its nest's name", async () => {
    const member = session("remote", "Member session", "Remote nest");
    rows.searchHits = [member];
    const { onPick, input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(await screen.findByRole("option", { name: /Remote nest/ })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onPick).toHaveBeenCalledWith(member);
  });

  it("matches the sidebar's loaded nests by name", () => {
    rows.loaded = [session("a", "Alpha", "Release prep"), session("b", "Beta", "Docs")];
    const { input } = renderPicker();

    fireEvent.change(input, { target: { value: "release" } });

    expect(screen.getByRole("option", { name: /Release prep/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Docs/ })).toBeNull();
  });
});
