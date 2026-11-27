import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";

import { MoveToNestPicker } from "./MoveToNestPicker";

const rows = vi.hoisted(() => ({
  loaded: [] as Conversation[],
  /** The search's pages, each fetched by Show more after the first. */
  searchPages: [[]] as Conversation[][],
  /** Searches fail until a Retry, once this many times. */
  failures: 0,
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const { useState } = await import("react");
  return {
    ...(await importOriginal<typeof ConversationsModule>()),
    // A stand-in for the paged search: Show more loads the next page and Retry refetches.
    useSessionSearch: (query: string) => {
      const [loadedPages, setLoadedPages] = useState(1);
      const [failed, setFailed] = useState(() => rows.failures > 0);
      if (!query) return { data: undefined, hasNextPage: false, isError: false };
      return {
        data: failed
          ? undefined
          : { pages: rows.searchPages.slice(0, loadedPages).map((data) => ({ data })) },
        isError: failed,
        isFetching: false,
        hasNextPage: !failed && loadedPages < rows.searchPages.length,
        fetchNextPage: () => setLoadedPages((count) => count + 1),
        refetch: () => {
          rows.failures -= 1;
          setFailed(rows.failures > 0);
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
  rows.searchPages = [[]];
  rows.failures = 0;
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

  it("matches the sidebar's loaded nests by name", () => {
    rows.loaded = [session("a", "Alpha", "Release prep"), session("b", "Beta", "Docs")];
    const { input } = renderPicker();

    fireEvent.change(input, { target: { value: "release" } });

    expect(screen.getByRole("option", { name: /Release prep/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Docs/ })).toBeNull();
  });

  it("reaches a nest that only a later page of the search finds, through Show more", () => {
    const later = session("later", "Later member", "Later nest");
    rows.searchPages = [[session("solo", "Member alone")], [later]];
    const { onPick, input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(screen.queryByRole("option", { name: /Later nest/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    fireEvent.click(screen.getByRole("option", { name: /Later nest/ }));

    expect(onPick).toHaveBeenCalledWith(later);
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("says a failed search failed, not that there are no nests, and retries it", () => {
    const member = session("remote", "Member session", "Remote nest");
    rows.searchPages = [[member]];
    rows.failures = 1;
    const { input } = renderPicker();

    fireEvent.change(input, { target: { value: "Member" } });
    expect(screen.getByText("Couldn't search for nests.")).toHaveAttribute("role", "status");
    expect(screen.queryByText("No other nests yet.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(screen.getByRole("option", { name: /Remote nest/ })).toBeInTheDocument();
    expect(screen.queryByText("Couldn't search for nests.")).toBeNull();
  });
});
