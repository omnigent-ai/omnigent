import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

import { MoveToNestPicker } from "./MoveToNestPicker";

// The server's title search: its pages, how many have loaded, and whether it has failed.
const rows = vi.hoisted(() => ({
  loaded: [] as Conversation[],
  searchPages: [] as Conversation[][],
  pagesLoaded: 1,
  failing: false,
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const { useReducer } = await import("react");
  return {
    ...(await importOriginal<typeof ConversationsModule>()),
    useSessionSearch: (query: string) => {
      const [, rerender] = useReducer((count: number) => count + 1, 0);
      const answered = query !== "" && !rows.failing;
      return {
        data: answered
          ? { pages: rows.searchPages.slice(0, rows.pagesLoaded).map((data) => ({ data })) }
          : undefined,
        isError: query !== "" && rows.failing,
        isFetching: false,
        hasNextPage: answered && rows.pagesLoaded < rows.searchPages.length,
        fetchNextPage: () => {
          rows.pagesLoaded += 1;
          rerender();
        },
        refetch: () => {
          rows.failing = false;
          rerender();
        },
      };
    },
  };
});
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
  rows.searchPages = [];
  rows.pagesLoaded = 1;
  rows.failing = false;
});

describe("MoveToNestPicker", () => {
  it("keeps a search hit on a member's title, offered under its nest's name", async () => {
    const member = session("remote", "Member session", "Remote nest");
    rows.searchPages = [[member]];
    const { onPick, input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(await screen.findByRole("option", { name: /Remote nest/ })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onPick).toHaveBeenCalledWith(member);
  });

  it("shows more search results when the first page holds no other nest", async () => {
    const member = session("remote", "Member session", "Remote nest");
    // Solo sessions and the session itself match the title but aren't nests to move into.
    rows.searchPages = [[session("solo", "Member notes"), SESSION], [member]];
    const { onPick, input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(screen.queryByRole("option", { name: /Remote nest/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show more nests" }));

    expect(await screen.findByRole("option", { name: /Remote nest/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more nests" })).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith(member);
  });

  it("says a failed search failed, and finds the nest on Retry", async () => {
    const member = session("remote", "Member session", "Remote nest");
    rows.searchPages = [[member]];
    rows.failing = true;
    const { input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(screen.getByText("Couldn't search your nests.")).toHaveAttribute("role", "status");
    expect(screen.queryByText("No other nests yet.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("option", { name: /Remote nest/ })).toBeInTheDocument();
    expect(screen.queryByText("Couldn't search your nests.")).toBeNull();
  });

  it("matches the sidebar's loaded nests by name", () => {
    rows.loaded = [session("a", "Alpha", "Release prep"), session("b", "Beta", "Docs")];
    const { input } = renderPicker();

    fireEvent.change(input, { target: { value: "release" } });

    expect(screen.getByRole("option", { name: /Release prep/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Docs/ })).toBeNull();
  });
});
