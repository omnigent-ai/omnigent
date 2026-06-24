import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAvailableAgents } from "./useAvailableAgents";

// The hook unions the built-in agent list from GET /v1/agents with
// custom agents discovered on the caller's sessions via
// GET /v1/sessions?limit=100&kind=any (enriched per-agent through
// GET /v1/sessions/{id}/agent). `authenticatedFetch` passes through to
// the global `fetch` when no user id is set (the default in jsdom), so
// stubbing `fetch` exercises the real fetch + mapping path rather than
// a hand-rolled stand-in. The two top-level fetches run in parallel
// (Promise.all), so the stub is keyed by URL, not by call order.
function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

const BUILTINS_URL = "/v1/agents";
const SCAN_URL = "/v1/sessions?limit=100&kind=any";

/**
 * Stub the global fetch with per-URL responses. Unrouted URLs reject
 * loudly so an unexpected request fails the test instead of hanging
 * TanStack's retry loop.
 */
function routeFetch(routes: Record<string, Response>) {
  fetchMock.mockImplementation((url: string) => {
    const route = routes[url];
    if (!route) {
      return Promise.reject(new Error(`unrouted fetch in test: ${url}`));
    }
    return Promise.resolve(route);
  });
}

function wrapper({ children }: { children: ReactNode }) {
  // retry off so the no-network/error case resolves on the first
  // attempt instead of stalling the test on TanStack's backoff.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const EMPTY_SCAN = mockResponse({ object: "list", data: [], has_more: false });

describe("useAvailableAgents", () => {
  it("does not fetch while disabled", async () => {
    const { result } = renderHook(() => useAvailableAgents({ enabled: false }), { wrapper });
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("fetches built-ins from /v1/agents and scans /v1/sessions?kind=any", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({ object: "list", data: [], has_more: false }),
      [SCAN_URL]: EMPTY_SCAN,
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Pins both source endpoints. /v1/agents drifting back to the
    // retired /api/agents route would break against current servers;
    // the scan dropping kind=any would silently stop discovering
    // agents bound only to sub-agent sessions.
    const urls = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(urls).toContain(BUILTINS_URL);
    expect(urls).toContain(SCAN_URL);
  });

  it("maps rows into AvailableAgent and applies native, nessie, and debby display names", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({
        object: "list",
        data: [
          {
            id: "ag_native",
            name: "claude-native-ui",
            description: null,
            harness: "claude-native",
          },
          {
            id: "ag_pi_native",
            name: "pi-native-ui",
            description: null,
            harness: "pi-native",
          },
          {
            id: "ag_opencode_native",
            name: "opencode-native-ui",
            description: null,
            harness: "opencode-native",
          },
          {
            id: "ag_nessie",
            name: "nessie",
            description: "Multi-agent coding orchestrator.",
            harness: "nessie",
            skills: [{ name: "review-pr", description: "Review a pull request" }],
          },
          {
            id: "ag_debby",
            name: "debby",
            description: "A two-headed brainstorming partner.",
            harness: "claude-sdk",
          },
          {
            id: "ag_yaml",
            name: "databricks_coding_agent",
            description: "A coding agent",
            harness: "codex",
            // os_env.cwd pass-through (#509): the new-session picker
            // defaults the workspace field to this value.
            default_workspace: "/opt/custom",
          },
        ],
        has_more: false,
      }),
      [SCAN_URL]: EMPTY_SCAN,
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Native terminal wrappers show product names ("Claude Code" / "Pi").
    // nessie's and debby's lowercase slugs are
    // title-cased to "Nessie" / "Debby". A regression in DISPLAY_NAMES
    // would surface the raw slug to users. Other agents pass their name through as the
    // display name. `harness` is passed through verbatim so the picker
    // can pick a glyph by kind — a custom Codex agent (ag_yaml) keeps
    // its "codex" harness even though its name doesn't say "codex".
    // `skills` passes through verbatim (nessie) and normalises to []
    // when the wire field is absent (older servers) — the landing
    // composer's "/" menu indexes it unconditionally.
    expect(result.current.data).toEqual([
      {
        id: "ag_native",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
        default_workspace: null,
      },
      {
        id: "ag_pi_native",
        name: "pi-native-ui",
        display_name: "Pi",
        description: null,
        harness: "pi-native",
        skills: [],
        default_workspace: null,
      },
      {
        id: "ag_opencode_native",
        name: "opencode-native-ui",
        display_name: "OpenCode",
        description: null,
        harness: "opencode-native",
        skills: [],
      },
      {
        id: "ag_nessie",
        name: "nessie",
        display_name: "Nessie",
        description: "Multi-agent coding orchestrator.",
        harness: "nessie",
        skills: [{ name: "review-pr", description: "Review a pull request" }],
        default_workspace: null,
      },
      {
        id: "ag_debby",
        name: "debby",
        display_name: "Debby",
        description: "A two-headed brainstorming partner.",
        harness: "claude-sdk",
        skills: [],
        default_workspace: null,
      },
      {
        id: "ag_yaml",
        name: "databricks_coding_agent",
        display_name: "Databricks_coding_agent",
        description: "A coding agent",
        harness: "codex",
        skills: [],
        default_workspace: "/opt/custom",
      },
    ]);
  });

  it("defaults a missing harness to null", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({
        object: "list",
        // `harness` omitted — the server leaves it off when the agent's
        // spec couldn't be loaded. It must normalise to null so the card
        // falls back to the generic glyph instead of leaking undefined.
        data: [{ id: "ag_x", name: "x" }],
        has_more: false,
      }),
      [SCAN_URL]: EMPTY_SCAN,
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.[0].harness).toBeNull();
  });

  it("defaults a missing description to null", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({
        object: "list",
        // `description` omitted entirely (not just null) — the picker
        // renders the description conditionally, so undefined must be
        // normalised to null rather than leaking through.
        data: [{ id: "ag_x", name: "x" }],
        has_more: false,
      }),
      [SCAN_URL]: EMPTY_SCAN,
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.[0].description).toBeNull();
  });

  it("surfaces an error when the built-in request fails", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({ detail: "nope" }, { ok: false, status: 500 }),
      [SCAN_URL]: EMPTY_SCAN,
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toContain("500");
  });

  it("discovers custom session-bound agents and drops built-in shadows", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({
        object: "list",
        data: [{ id: "ag_native", name: "claude-native-ui", harness: "claude-native" }],
        has_more: false,
      }),
      [SCAN_URL]: mockResponse({
        object: "list",
        data: [
          // Binds the built-in's own agent row — dropped by id.
          { id: "conv_1", agent_id: "ag_native", agent_name: "claude-native-ui" },
          // A fork clone of the built-in — distinct id, but the clone
          // suffix strips back to a built-in name, so dropped by name.
          {
            id: "conv_2",
            agent_id: "ag_clone",
            agent_name: "claude-native-ui (fork conv_9)",
          },
          // A fork OF A fork of the built-in — nested clone suffixes. A
          // single-layer strip leaves "claude-native-ui (fork conv_9)"
          // (not a built-in name), so the clone leaks into the picker;
          // once enriched its claude-native harness resolves to the
          // "Claude Code" display name, surfacing as a DUPLICATE of the
          // built-in. agentRootName peels every layer so it drops by
          // name before it is ever enriched.
          {
            id: "conv_6",
            agent_id: "ag_clone2",
            agent_name: "claude-native-ui (fork conv_9) (fork conv_10)",
          },
          // Genuinely custom agent; survives and is enriched below.
          { id: "conv_3", agent_id: "ag_doc", agent_name: "doc-writer" },
          // Same custom agent on an older session — deduped by id, and
          // the enrich fetch must use the newest session (conv_3).
          { id: "conv_4", agent_id: "ag_doc", agent_name: "doc-writer" },
          // Orphaned row (agent deleted) — skipped.
          { id: "conv_5", agent_id: "ag_gone", agent_name: null },
        ],
        has_more: false,
      }),
      "/v1/sessions/conv_3/agent": mockResponse({
        id: "ag_doc",
        object: "agent",
        name: "doc-writer",
        description: "Documentation specialist",
        harness: "claude-sdk",
        skills: [{ name: "humanizer", description: "Remove AI writing patterns" }],
      }),
      // Reached only if the fork-of-fork leaks (i.e. the fix regressed):
      // its claude-native harness would resolve to "Claude Code", proving
      // the leak renders as a duplicate built-in. With the fix conv_6 is
      // dropped before enrichment, so this mock is never hit.
      "/v1/sessions/conv_6/agent": mockResponse({
        id: "ag_clone2",
        object: "agent",
        name: "claude-native-ui (fork conv_9) (fork conv_10)",
        harness: "claude-native",
        skills: [],
      }),
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // One built-in + one custom. A second "Claude Code" row (from
    // ag_clone/ag_clone2 leaking) means shadow-dropping regressed —
    // ag_clone2 specifically guards the nested fork-of-fork case that
    // surfaces as a duplicate built-in; ag_doc missing means kind=any
    // discovery broke; two ag_doc rows mean the by-id dedup broke.
    expect(result.current.data).toEqual([
      {
        id: "ag_native",
        name: "claude-native-ui",
        display_name: "Claude Code",
        description: null,
        harness: "claude-native",
        skills: [],
        default_workspace: null,
      },
      {
        id: "ag_doc",
        name: "doc-writer",
        display_name: "Doc-writer",
        description: "Documentation specialist",
        harness: "claude-sdk",
        skills: [{ name: "humanizer", description: "Remove AI writing patterns" }],
        default_workspace: null,
      },
    ]);
    // The enrich fetch ran once, against the newest session the agent
    // was seen on — not the older duplicate (conv_4) and not the
    // shadowed rows (which must not be enriched at all).
    const enrichCalls = fetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.endsWith("/agent"));
    expect(enrichCalls).toEqual(["/v1/sessions/conv_3/agent"]);
  });

  it("collapses same-named custom agents with distinct agent_ids to the newest session's row", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({ object: "list", data: [], has_more: false }),
      [SCAN_URL]: mockResponse({
        object: "list",
        data: [
          // Three sessions of the same custom agent, each with its own
          // agent_id — a local-YAML agent mints a fresh row per launch
          // (#3234). Scan order is newest-first, so conv_new wins.
          { id: "conv_new", agent_id: "ag_run3", agent_name: "elise_working_agent" },
          { id: "conv_mid", agent_id: "ag_run2", agent_name: "elise_working_agent" },
          // A fork clone of the custom agent strips back to the same
          // base name, so it collapses into the same row too.
          {
            id: "conv_old",
            agent_id: "ag_run1",
            agent_name: "elise_working_agent (fork conv_7)",
          },
          // A differently-named custom agent must NOT be collapsed —
          // the dedup keys on base name, not on "is custom".
          { id: "conv_doc", agent_id: "ag_doc", agent_name: "doc-writer" },
        ],
        has_more: false,
      }),
      // Only the newest session per name may be enriched. An enrich
      // fetch for conv_mid/conv_old is unrouted and rejects loudly,
      // failing the test if the by-name collapse regresses.
      "/v1/sessions/conv_new/agent": mockResponse({
        id: "ag_run3",
        object: "agent",
        name: "elise_working_agent",
        description: "Elise's agent",
        harness: "claude-sdk",
      }),
      "/v1/sessions/conv_doc/agent": mockResponse({
        id: "ag_doc",
        object: "agent",
        name: "doc-writer",
        description: null,
        harness: "codex",
      }),
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Exactly one elise row (the newest mint, ag_run3) plus doc-writer.
    // Three elise rows would mean the by-name collapse regressed to
    // by-id-only dedup; zero would mean customs were over-collapsed.
    expect(result.current.data).toEqual([
      {
        id: "ag_run3",
        name: "elise_working_agent",
        display_name: "Elise_working_agent",
        description: "Elise's agent",
        harness: "claude-sdk",
        skills: [],
        default_workspace: null,
      },
      {
        id: "ag_doc",
        name: "doc-writer",
        display_name: "Doc-writer",
        description: null,
        harness: "codex",
        skills: [],
        default_workspace: null,
      },
    ]);
  });

  it("degrades to built-ins when the sessions scan fails", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({
        object: "list",
        data: [{ id: "ag_native", name: "claude-native-ui" }],
        has_more: false,
      }),
      // Transient 5xx on the scan — built-in availability must not be
      // hostage to the discovery extension, so the hook still succeeds.
      [SCAN_URL]: mockResponse({ detail: "boom" }, { ok: false, status: 503 }),
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.map((a) => a.id)).toEqual(["ag_native"]);
  });

  it("lists a custom agent with scan fields when its enrich fetch fails", async () => {
    routeFetch({
      [BUILTINS_URL]: mockResponse({ object: "list", data: [], has_more: false }),
      [SCAN_URL]: mockResponse({
        object: "list",
        data: [{ id: "conv_3", agent_id: "ag_doc", agent_name: "doc-writer" }],
        has_more: false,
      }),
      // The agent's bundle can't be loaded (or the fetch 500s) — the
      // agent must still be listed from scan fields, mirroring the
      // server's own spec-load degradation, just without harness/skills.
      "/v1/sessions/conv_3/agent": mockResponse({ detail: "boom" }, { ok: false, status: 500 }),
    });

    const { result } = renderHook(() => useAvailableAgents(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual([
      {
        id: "ag_doc",
        name: "doc-writer",
        display_name: "Doc-writer",
        description: null,
        harness: null,
        skills: [],
      },
    ]);
  });
});
