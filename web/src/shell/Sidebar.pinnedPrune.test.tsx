// Pruning of dead pins on a multi-user server. A pinned session that was
// deleted elsewhere 404s on backfill (no row ever comes back); the normalize
// effect must still settle and drop it, rather than treating "no row" as
// "still in flight" and lingering the dead pin forever.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
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

// A deleted pin ("gone") 404s on backfill: no row, but its query has settled.
vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({ mutate: vi.fn() }),
  usePinnedConversationBackfill: () => ({ conversations: [], settledIds: new Set(["gone"]) }),
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

describe("pruning a deleted pin whose backfill 404s", () => {
  it("drops the dead pin once its backfill query has settled", async () => {
    // Pins "gone" (deleted elsewhere) and "loaded" (present). "gone" never
    // loads and its backfill 404s, so it only shows up in settledIds.
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["gone", "loaded"]));
    stubPreferencesFetch({ pinned_conversation_ids: ["gone", "loaded"] });
    mockConversations([conv("loaded")]);

    renderAt("/");

    // The dead pin is pruned from the local cache; the live pin stays.
    await waitFor(() =>
      expect(
        JSON.parse(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY) ?? "null"),
      ).toEqual(["loaded"]),
    );
  });
});
