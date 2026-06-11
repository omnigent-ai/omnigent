// Unit tests for `permissionsApi.ts` — happy-path requests with mocked
// `fetch`, plus error-path coverage that pins the server's structured
// error shape (`{error: {code, message}}`) into the thrown Error.
//
// Mirrors the pattern used by `sessionsApi.test.ts` (one fetchMock per
// test, vi.stubGlobal/unstubAllGlobals around it).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";
import * as identity from "./identity";
import {
  LEVEL_OWNER,
  derivePermissionLevel,
  grantPermission,
  isOwnerLevel,
  listPermissions,
  revokePermission,
} from "./permissionsApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listPermissions", () => {
  it("GETs /v1/sessions/{id}/permissions and returns the array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse([
        { user_id: "alice", conversation_id: "conv_abc", level: 3 },
        { user_id: "bob", conversation_id: "conv_abc", level: 1 },
      ]),
    );

    const result = await listPermissions("conv_abc");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit | undefined];
    expect(url).toBe("/v1/sessions/conv_abc/permissions");
    // GET — no method or undefined method are both valid.
    expect(init?.method ?? "GET").toBe("GET");
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({
      user_id: "alice",
      conversation_id: "conv_abc",
      level: 3,
    });
  });

  it("url-encodes the session id", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse([]));
    await listPermissions("conv with space");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv%20with%20space/permissions");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(listPermissions("missing")).rejects.toThrow(/404/);
  });
});

describe("grantPermission", () => {
  it("PUTs /v1/sessions/{id}/permissions with user_id and level", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ user_id: "bob", conversation_id: "conv_abc", level: 2 }),
    );

    const result = await grantPermission("conv_abc", "bob", 2);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/permissions");
    expect(init.method).toBe("PUT");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ user_id: "bob", level: 2 });
    expect(result).toEqual({
      user_id: "bob",
      conversation_id: "conv_abc",
      level: 2,
    });
  });

  it.each([
    [1, "read"],
    [2, "edit"],
    [3, "manage"],
  ])("forwards level %i (%s) verbatim", async (level) => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ user_id: "u", conversation_id: "c", level }),
    );
    await grantPermission("c", "u", level);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string).level).toBe(level);
  });

  it("uses the structured error message when present", async () => {
    // The server returns OmnigentError as
    // `{error: {code: "forbidden", message: "..."}}` — surface
    // `message` in the thrown Error so callers display it directly.
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "forbidden", message: "'rice' needs manage permission" } },
        { ok: false, status: 403 },
      ),
    );
    await expect(grantPermission("conv_abc", "u", 2)).rejects.toThrow(
      "'rice' needs manage permission",
    );
  });

  it("falls back to status text on malformed error body", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    await expect(grantPermission("conv_abc", "u", 2)).rejects.toThrow(/500/);
  });

  it("url-encodes the session id", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ user_id: "u", conversation_id: "x", level: 1 }),
    );
    await grantPermission("conv with space", "u", 1);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv%20with%20space/permissions");
  });
});

describe("revokePermission", () => {
  it("DELETEs /v1/sessions/{id}/permissions/{user_id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { status: 204 }));

    await revokePermission("conv_abc", "alice");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/permissions/alice");
    expect(init.method).toBe("DELETE");
  });

  it("treats 204 as success even though the body is empty", async () => {
    // The server returns 204 No Content on revoke — there's nothing
    // to parse. The helper must not throw.
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 204,
      statusText: "No Content",
      json: async () => null,
    } as unknown as Response);
    await expect(revokePermission("c", "u")).resolves.toBeUndefined();
  });

  it("url-encodes both the session id and the user id", async () => {
    // user_ids are emails post-Databricks-Apps integration, which contain
    // `@` — must be percent-encoded so the path doesn't break.
    fetchMock.mockResolvedValueOnce(mockResponse(null, { status: 204 }));
    await revokePermission("conv_abc", "alice@example.com");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_abc/permissions/alice%40example.com",
    );
  });

  it("uses the structured error message on 403", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "forbidden", message: "Cannot modify your own permissions" } },
        { ok: false, status: 403 },
      ),
    );
    await expect(revokePermission("conv_abc", "self")).rejects.toThrow(
      "Cannot modify your own permissions",
    );
  });
});

describe("derivePermissionLevel — resolution order", () => {
  function makeSession(permissionLevel: number | null): Session {
    return {
      id: "conv_test",
      agentId: "ag_test",
      agentName: null,
      runnerId: null,
      status: "idle",
      createdAt: 0,
      title: null,
      labels: {},
      items: [],
      pendingElicitations: [],
      permissionLevel,
      parentSessionId: null,
      subAgentName: null,
    };
  }

  function makeConv(permissionLevel: number | null): Conversation {
    return {
      id: "conv_test",
      object: "conversation",
      title: null,
      created_at: 0,
      updated_at: 0,
      labels: {},
      permission_level: permissionLevel,
    };
  }

  it("prefers session.permissionLevel over the sidebar row", () => {
    // Authoritative source. Sub-agent (child) sessions only get their
    // level here — the sidebar list filters them out — so this branch
    // is what unblocks interaction with children.
    const session = makeSession(4);
    const sidebar = makeConv(1);
    expect(derivePermissionLevel(session, false, sidebar, "conv_test", true)).toBe(4);
  });

  it("returns null when the session snapshot says null (permissive UI default)", () => {
    // Children currently come back with permission_level=null from the
    // backend because the level helper doesn't delegate to parent. The
    // UI treats null as permissive, so we surface it verbatim.
    const session = makeSession(null);
    expect(derivePermissionLevel(session, false, null, "conv_child", true)).toBeNull();
  });

  it("falls back to the sidebar row when the session snapshot is not yet available", () => {
    // Steady-state path for top-level conversations: the sidebar list
    // has been resolved long before the user picked the chat, so its
    // level renders synchronously while the single-fetch is in flight.
    const sidebar = makeConv(2);
    expect(derivePermissionLevel(null, true, sidebar, "conv_test", true)).toBe(2);
  });

  it("returns null while the single-fetch is still loading and the sidebar has no row", () => {
    // Child session case before the single-fetch resolves: don't flash
    // read-only just because the sidebar doesn't know about this conv.
    expect(derivePermissionLevel(null, true, null, "conv_child", true)).toBeNull();
  });

  it("falls back to read-only (1) when nothing knows about the conversation", () => {
    // The conversations list resolved (so we have a snapshot of the
    // user's accessible sessions), but neither the sidebar nor the
    // single-fetch returned a row — likely a deleted or unauthorized
    // conversation. Read-only is the safe default the UI gates on.
    expect(derivePermissionLevel(null, false, null, "conv_unknown", true)).toBe(1);
  });

  it("returns null when there is no conversation id at all", () => {
    // ``/`` (home) — nothing to gate on.
    expect(derivePermissionLevel(null, false, null, undefined, true)).toBeNull();
  });

  it("returns null while the conversations list is still loading", () => {
    // Cold boot. We can't conclude "unknown conversation → read-only"
    // until the sidebar has settled; that would briefly disable the
    // composer on every load.
    expect(derivePermissionLevel(null, false, null, "conv_test", false)).toBeNull();
  });
});

describe("isOwnerLevel — owner boundary", () => {
  it("treats the owner level and above as owner", () => {
    expect(isOwnerLevel(LEVEL_OWNER)).toBe(true);
    expect(isOwnerLevel(LEVEL_OWNER + 1)).toBe(true);
  });

  it("treats read / edit / manage (below owner) as non-owner", () => {
    // These are the collaborator levels that must NOT be allowed to
    // type into the shared terminal — they attach read-only instead.
    expect(isOwnerLevel(1)).toBe(false);
    expect(isOwnerLevel(2)).toBe(false);
    expect(isOwnerLevel(3)).toBe(false);
  });

  it("treats null (single-user / unresolved) permissively as owner", () => {
    // Matches derivePermissionLevel/useCanEdit: a null level means
    // permissions are off or still loading, so don't lock the owner out.
    expect(isOwnerLevel(null)).toBe(true);
  });
});

describe("authenticatedFetch integration", () => {
  it("all API functions route through authenticatedFetch, not raw fetch", async () => {
    const spy = vi.spyOn(identity, "authenticatedFetch").mockResolvedValue(
      mockResponse([]),
    );

    await listPermissions("conv_abc");
    expect(spy).toHaveBeenCalledTimes(1);

    spy.mockResolvedValue(mockResponse({ user_id: "u", conversation_id: "c", level: 1 }));
    await grantPermission("c", "u", 1);
    expect(spy).toHaveBeenCalledTimes(2);

    spy.mockResolvedValue(mockResponse(null, { status: 204 }));
    await revokePermission("c", "u");
    expect(spy).toHaveBeenCalledTimes(3);

    spy.mockRestore();
  });
});
