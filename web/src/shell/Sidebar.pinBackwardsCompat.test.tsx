// End-to-end backwards-compatibility test for the server-side pin migration.
//
// Scenario (the reported failure): a user pins a session on the OLD build
// (localStorage-only), then the UI is upgraded BEFORE the server, then the
// server is upgraded. The pin must survive every step — it must never be wiped
// during the UI-before-server window.
//
// Unlike Sidebar.pinMigration.test.tsx (which mocks the query), this drives the
// REAL `usePinnedConversations` + `useMigrateLocalPinsToServer` +
// `setConversationPinned` against a fake `/v1/sessions` server that flips from
// old (ignores `?pinned=true`) to new (honors it + stores a per-user pin), with
// a fresh QueryClient per stage to model a page reload.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePinnedConversations, useTogglePinnedConversation } from "@/hooks/useConversations";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { useMigrateLocalPinsToServer } from "./Sidebar";
import { PINNED_CONVERSATION_IDS_STORAGE_KEY } from "./sidebarNav";

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

// ── Fake server ───────────────────────────────────────────────────────────
//
// Models the only two behaviours that matter for this scenario:
//   old → ignores the `pinned` param, returns the full (unfiltered) page, and
//         no row carries the `omnigent.pinned` label (pre-feature server).
//   new → honors `?pinned=true`, returns only the caller's pinned rows with the
//         label collapsed to the bare `omnigent.pinned` key, and PATCH stores /
//         clears the per-user pin.
const server = {
  mode: "old" as "old" | "new",
  // Per-user pins the NEW server stores + surfaces (omnigent.pinned.<user>).
  perUserPins: new Set<string>(),
  // Bare-key pins an OLD server stores from a PATCH. Faithful to the bug: the
  // new server drops any bare `omnigent.pinned` key on read (only its per-user
  // key is surfaced), so a bare-key pin written pre-upgrade is LOST on upgrade.
  barePins: new Set<string>(),
  // every session that exists (what an old server returns unfiltered).
  allSessions: ["chat_1", "chat_2"],
  patchCount: 0,
};

function row(id: string, pinned: boolean) {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 1,
    labels: pinned ? { [PINNED_LABEL_KEY]: "1721760000000" } : {},
  };
}

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => body } as unknown as Response;
}

const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
  const url = input.toString();
  // PATCH /v1/sessions/{id} — pin/unpin. Both servers accept it; they differ in
  // how they store it (per-user on new, bare on old).
  if (init?.method === "PATCH") {
    server.patchCount += 1;
    const id = decodeURIComponent(url.split("/v1/sessions/")[1]);
    const body = JSON.parse(init.body as string) as { labels?: Record<string, string> };
    const val = body.labels?.[PINNED_LABEL_KEY];
    const store = server.mode === "new" ? server.perUserPins : server.barePins;
    if (val === "" || val == null) store.delete(id);
    else store.add(id);
    return Promise.resolve(jsonResponse(row(id, store.has(id))));
  }
  // GET /v1/sessions?...&pinned=true — the pinned list.
  if (server.mode === "old") {
    // Old server drops the unknown `pinned` param and returns everything,
    // WITHOUT any pin label (a bare-key pin isn't surfaced as omnigent.pinned
    // on the pre-feature read path).
    return Promise.resolve(jsonResponse({ data: server.allSessions.map((id) => row(id, false)) }));
  }
  // New server: only per-user pins are surfaced (bare pins are dropped on read).
  return Promise.resolve(
    jsonResponse({ data: [...server.perUserPins].map((id) => row(id, true)) }),
  );
});

// The union the sidebar renders: server pins ∪ leftover localStorage pins.
function readLegacyPins(): string[] {
  const raw = localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
  return raw ? (JSON.parse(raw) as string[]) : [];
}

// Captures the live toggle mutation so a test can pin/unpin through the REAL
// `useTogglePinnedConversation` (including its old-server localStorage fallback)
// while the harness is mounted.
let toggle: ((args: { id: string; pinned: boolean }) => void) | null = null;

// Minimal harness wiring the real hooks exactly as the Sidebar does, exposing
// the union pinned set so assertions read "what the user sees as pinned".
function Harness() {
  const { data, isSuccess } = usePinnedConversations();
  const serverIds = (data?.conversations ?? []).map((c) => c.id);
  useMigrateLocalPinsToServer(new Set(serverIds), isSuccess, data?.filterHonored ?? false);
  const toggleMutation = useTogglePinnedConversation();
  toggle = toggleMutation.mutate;
  const union = new Set(serverIds);
  for (const id of readLegacyPins()) union.add(id);
  return createElement("div", { "data-testid": "pinned" }, [...union].sort().join(","));
}

function loadPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return render(createElement(Harness), { wrapper });
}

beforeEach(() => {
  server.mode = "old";
  server.perUserPins = new Set();
  server.barePins = new Set();
  server.patchCount = 0;
  toggle = null;
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("pin survives a UI-before-server upgrade", () => {
  it("keeps a pin pinned across old→new server without ever losing it", async () => {
    // Pre-state: the user pinned `chat_1` on the OLD build (localStorage only).
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));

    // ── Stage 1: UI upgraded, server still OLD ────────────────────────────
    const stage1 = loadPage();
    // The pin shows immediately (union with localStorage).
    await waitFor(() => expect(stage1.getByTestId("pinned").textContent).toBe("chat_1"));
    // Migration stayed inert: no writes, legacy key intact — so the eventual
    // server upgrade still has the pin to migrate instead of finding it gone.
    expect(server.patchCount).toBe(0);
    expect(readLegacyPins()).toEqual(["chat_1"]);
    stage1.unmount();

    // ── Stage 2: server upgraded, page reloaded ───────────────────────────
    server.mode = "new"; // now honors ?pinned=true (currently no server pins)
    const stage2 = loadPage();
    // Migration fires and pushes the local pin to the server.
    await waitFor(() => expect(server.perUserPins.has("chat_1")).toBe(true));
    expect(server.patchCount).toBe(1);
    // Legacy key is cleared once the write is confirmed.
    await waitFor(() =>
      expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBeNull(),
    );
    // Still pinned throughout (server cache patch keeps it in the union).
    await waitFor(() => expect(stage2.getByTestId("pinned").textContent).toBe("chat_1"));
    stage2.unmount();

    // ── Stage 3: subsequent reload on the NEW server ──────────────────────
    const stage3 = loadPage();
    // Pin is now server-authoritative; it persists with nothing left in
    // localStorage and no further migration writes.
    await waitFor(() => expect(stage3.getByTestId("pinned").textContent).toBe("chat_1"));
    expect(server.patchCount).toBe(1);
    expect(readLegacyPins()).toEqual([]);
  });

  it("does not lose the pin even if the server is upgraded much later (repeated old-server loads)", async () => {
    localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["chat_1"]));

    // Several reloads while the server is still old — each must be a no-op that
    // preserves the pin (the failure report was the pin vanishing here). The
    // reloads are inherently sequential (each models a fresh page load).
    for (let i = 0; i < 3; i++) {
      const r = loadPage();
      // eslint-disable-next-line no-await-in-loop
      await waitFor(() => expect(r.getByTestId("pinned").textContent).toBe("chat_1"));
      expect(server.patchCount).toBe(0);
      expect(readLegacyPins()).toEqual(["chat_1"]);
      r.unmount();
    }

    // Finally the server catches up.
    server.mode = "new";
    const final = loadPage();
    await waitFor(() => expect(server.perUserPins.has("chat_1")).toBe(true));
    await waitFor(() => expect(final.getByTestId("pinned").textContent).toBe("chat_1"));
  });

  it("keeps a pin created DURING the UI-before-server window (toggle falls back to localStorage)", async () => {
    // No pre-existing pin. The user pins a session in the NEW UI while the
    // server is still OLD — the reported failure: the pin was PATCHed as a bare
    // key the upgraded server dropped, so it vanished on the server upgrade.

    // ── Stage 1: pin in the new UI, old server ────────────────────────────
    const stage1 = loadPage();
    await waitFor(() => expect(stage1.getByTestId("pinned").textContent).toBe(""));
    toggle!({ id: "chat_2", pinned: true });
    // The pin shows immediately (optimistic patch + localStorage union)...
    await waitFor(() => expect(stage1.getByTestId("pinned").textContent).toBe("chat_2"));
    // ...and crucially was NOT PATCHed to the old server (which would store a
    // bare key the upgraded server discards). It went to localStorage instead.
    expect(server.patchCount).toBe(0);
    expect(server.barePins.has("chat_2")).toBe(false);
    await waitFor(() => expect(readLegacyPins()).toEqual(["chat_2"]));
    stage1.unmount();

    // ── Stage 2: reload, still old server — pin persists ──────────────────
    const stage2 = loadPage();
    await waitFor(() => expect(stage2.getByTestId("pinned").textContent).toBe("chat_2"));
    expect(readLegacyPins()).toEqual(["chat_2"]);
    stage2.unmount();

    // ── Stage 3: server upgraded — the pin migrates instead of vanishing ──
    server.mode = "new";
    const stage3 = loadPage();
    await waitFor(() => expect(server.perUserPins.has("chat_2")).toBe(true));
    await waitFor(() =>
      expect(localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY)).toBeNull(),
    );
    await waitFor(() => expect(stage3.getByTestId("pinned").textContent).toBe("chat_2"));
  });
});
