// Server-backed pinned conversations (#2527): on a multi-user server the pin
// set is stored per-user on the server and rehydrates the sidebar, so pins
// survive a fresh browser (empty localStorage) and follow the user across
// devices. localStorage remains a local cache. These tests drive the Sidebar
// with a mocked /v1/preferences endpoint and assert the sidebar hydrates from
// it and writes pin toggles back to it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import { PINNED_CONVERSATION_IDS_STORAGE_KEY } from "@/shell/sidebarNav";

// Multi-user server → sidebar preferences sync is active.
vi.mock("@/lib/serverOrigin", () => ({
  isCurrentServerLocal: () => false,
  isLocalServerOrigin: () => false,
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => ({ conversations: [], settledIds: new Set() }),
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useProjectSessions: vi.fn(),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations, useProjectSessions } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);
const useProjectSessionsMock = vi.mocked(useProjectSessions);

function conv(id: string): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    agent_name: null,
  } as unknown as Conversation;
}

function mockConversations(convs: Conversation[]) {
  useConvMock.mockReturnValue({
    data: {
      pages: [{ data: convs, first_id: null, last_id: null, has_more: false }],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>);
}

/** Route the mocked fetch: GET returns `preferences`, PUT records the call. */
function stubPreferencesFetch(preferences: Record<string, unknown>) {
  const puts: { key: string; value: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (init?.method === "PUT") {
      const key = url.split("/v1/preferences/")[1] ?? "";
      const body = JSON.parse((init.body as string) ?? "{}");
      puts.push({ key, value: body.value });
      return { ok: true, status: 200, json: () => Promise.resolve({ object: "preference" }) };
    }
    return {
      ok: true,
      status: 200,
      json: () => Promise.resolve({ object: "preferences", preferences }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, puts };
}

function renderAt(initialEntry: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/" element={<Sidebar open onClose={vi.fn()} />} />
            <Route path="/c/:conversationId" element={<Sidebar open onClose={vi.fn()} />} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useConvMock.mockReset();
  useProjectSessionsMock.mockReset();
  useProjectSessionsMock.mockReturnValue({
    data: { pages: [{ data: [], first_id: null, last_id: null, has_more: false }] },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useProjectSessions>);
  localStorage.clear();
  vi.unstubAllGlobals();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("server-backed pinned conversations (#2527)", () => {
  it("hydrates the Pinned section from the server when localStorage is empty", async () => {
    // Fresh browser: no local pins. The server says session `srv` is pinned.
    stubPreferencesFetch({ pinned_conversation_ids: ["srv"] });
    mockConversations([conv("srv"), conv("other")]);

    renderAt("/");

    // Once the server copy resolves, the pinned row surfaces in the Pinned
    // section — its heading only renders when a pin exists.
    expect(await screen.findByText("Pinned")).toBeInTheDocument();
    // And the localStorage cache is backfilled from the server copy.
    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY) ?? "[]")).toEqual(
        ["srv"],
      ),
    );
  });

  it("adopts an explicitly empty server pin set instead of reseeding stale local pins", async () => {
    // Another device unpinned everything, so the server pin set is explicitly
    // []. A stale local cache must not resurrect the cleared pins by PUTting
    // them back — the empty server set is authoritative.
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["x"]));
    const { puts } = stubPreferencesFetch({ pinned_conversation_ids: [] });
    mockConversations([conv("x")]);

    renderAt("/");

    // The empty server set wins: the local cache is cleared to match it...
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY) ?? "null"),
      ).toEqual([]),
    );
    // ...and no write-back reseeds the cleared pin onto the server.
    expect(puts.find((p) => p.key === "pinned_conversation_ids")).toBeUndefined();
  });

  it("writes a pin toggle through to the server", async () => {
    // Server starts empty; the user pins a session, which must PUT the new set.
    const { puts } = stubPreferencesFetch({});
    mockConversations([conv("srv")]);

    renderAt("/");

    // Wait for the row, then click its pin control.
    const pinButton = await screen.findByRole("button", { name: /pin/i });
    pinButton.click();

    await waitFor(() => {
      const pinPut = puts.find((p) => p.key === "pinned_conversation_ids");
      expect(pinPut?.value).toEqual(["srv"]);
    });
  });
});
