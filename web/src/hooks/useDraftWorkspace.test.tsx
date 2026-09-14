import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";

import { markHostKeyless, clearHostKeyless } from "@/lib/sessionHost";
import { landingStorageKey } from "@/lib/landingStorage";
import {
  buildDraftTerminalAttachPath,
  createDraftWorkspaceContext,
  useDraftWorkspace,
  type DraftWorkspaceContext,
} from "./useDraftWorkspace";

const fetchMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: fetchMock,
}));

const STORAGE_KEY = landingStorageKey("omnigent:draft-workspace-contexts:v1");

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Failed",
    json: async () => body,
  } as Response;
}

function context(
  id: string,
  workspace: string,
  sessionId: string | null = null,
): Omit<DraftWorkspaceContext, "hostId" | "workspaceAliases"> {
  return { id, workspace, session_id: sessionId, lease_seconds: 600 };
}

function terminal(id = "terminal_bash_draft") {
  return {
    id,
    object: "workspace.resource",
    type: "terminal",
    name: "bash:draft",
    metadata: { terminal_name: "bash", session_key: "draft", running: true },
  };
}

function restoreContexts(...contexts: object[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(contexts));
}

function storedContext(id: string, workspace = "/repo", sessionId: string | null = null) {
  return { id, workspace, hostId: "host_1", leaseSeconds: 600, sessionId };
}

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("buildDraftTerminalAttachPath", () => {
  it("encodes opaque ids and includes read-only and host routing params", () => {
    expect(buildDraftTerminalAttachPath("context /1", "terminal /1", true, "host /1")).toBe(
      "/v1/hosts/host%20%2F1/workspace-contexts/context%20%2F1/resources/terminals/terminal%20%2F1/attach?read_only=true&omnigent_slice_key=host+%2F1",
    );
  });

  it("preserves HTTP host demotion when building an attachment", () => {
    markHostKeyless("draft_keyless");
    try {
      const path = buildDraftTerminalAttachPath("ctx", "term", true, "draft_keyless");
      expect(path).toBe(
        "/v1/hosts/draft_keyless/workspace-contexts/ctx/resources/terminals/term/attach?read_only=true",
      );
    } finally {
      clearHostKeyless("draft_keyless");
    }
  });
});

describe("draft workspace errors", () => {
  it("shows the host upgrade instruction while preserving the HTTP status", async () => {
    fetchMock.mockResolvedValue(
      response({ detail: "upgrade this host to enable workspace contexts" }, 409),
    );
    await expect(createDraftWorkspaceContext("older-host", "/repo")).rejects.toMatchObject({
      status: 409,
      message:
        "draft workspace create failed: 409 Failed — upgrade this host to enable workspace contexts",
    });
  });

  it("preserves an HTTP error when a proxy returns non-JSON", async () => {
    fetchMock.mockResolvedValue(
      new Response("Bad gateway", { status: 502, statusText: "Bad Gateway" }),
    );
    await expect(createDraftWorkspaceContext("host", "/repo")).rejects.toMatchObject({
      status: 502,
      message: "draft workspace create failed: 502 Bad Gateway",
    });
  });
});

describe("useDraftWorkspace", () => {
  it.each(["/repo", "/alias", "/other-alias"])(
    "preserves selected workspace %s when replacing an adopted draft",
    async (selectedWorkspace) => {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify([
          {
            id: "shared",
            workspace: "/repo",
            workspaceAliases: ["/repo", "/alias", "/other-alias"],
            hostId: "host_1",
            leaseSeconds: 600,
            sessionId: null,
          },
        ]),
      );
      let sharedSession: string | null = null;
      const sharedShells = [terminal("original")];
      const freshShells: ReturnType<typeof terminal>[] = [];
      fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
        if (url.endsWith("/shared/heartbeat"))
          return response(context("shared", "/repo", sharedSession));
        if (url.endsWith("/shared/handoff")) {
          sharedSession = "conv_1";
          return response(context("shared", "/repo", sharedSession));
        }
        if (url === "/v1/hosts/host_1/workspace-contexts")
          return response(context("fresh", "/repo"));
        if (url.endsWith("/resources/terminals")) {
          const shared = url.includes("/shared/");
          const shells = shared ? sharedShells : freshShells;
          if (init?.method === "POST") {
            const body = JSON.parse(String(init.body));
            if (body.session_id !== (shared ? sharedSession : null)) return response({}, 409);
            shells.push(terminal(shared ? "session_shell" : "draft_shell"));
            return response(shells.at(-1));
          }
          return response({ object: "list", data: [...shells] });
        }
        throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
      });
      const first = renderHook(
        ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
        { initialProps: { sessionId: null as string | null } },
      );
      const stale = renderHook(() => useDraftWorkspace(null));
      await waitFor(() => expect(first.result.current.terminals).toHaveLength(1));
      await waitFor(() => expect(stale.result.current.terminals).toHaveLength(1));
      await act(async () => {
        await first.result.current.adopt("conv_1");
      });
      first.rerender({ sessionId: "conv_1" });
      expect(stale.result.current.context?.session_id).toBeNull();

      await act(async () => {
        const captured = await stale.result.current.ensureContext("host_1", selectedWorkspace);
        expect((await stale.result.current.createTerminal(captured)).id).toBe("draft_shell");
      });
      expect(stale.result.current.context?.id).toBe("fresh");
      expect(stale.result.current.context?.workspaceAliases).toContain(selectedWorkspace);
      await act(async () => {
        expect((await stale.result.current.ensureContext("host_1", selectedWorkspace)).id).toBe(
          "fresh",
        );
      });
      expect(freshShells.map((shell) => shell.id)).toEqual(["draft_shell"]);
      expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "DELETE")).toEqual([]);
      expect(sharedShells.map((shell) => shell.id)).toEqual(["original"]);
      await act(async () => {
        expect((await first.result.current.createTerminal()).id).toBe("session_shell");
      });
      expect(sharedShells.map((shell) => shell.id)).toEqual(["original", "session_shell"]);
      expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ id: "shared", sessionId: "conv_1" }),
          expect.objectContaining({ id: "fresh", sessionId: null }),
        ]),
      );
    },
  );

  it("reconciles a stale draft close while allowing the session's confirmed close", async () => {
    restoreContexts(storedContext("shared"));
    let owner: string | null = null;
    let running = true;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/heartbeat")) return response(context("shared", "/repo", owner));
      if (url.endsWith("/handoff")) {
        owner = "conv_1";
        return response(context("shared", "/repo", owner));
      }
      if (url.endsWith("/resources/terminals"))
        return response({ object: "list", data: running ? [terminal("shared_shell")] : [] });
      if (init?.method === "DELETE") {
        const expectedSession = new URL(url, "http://local.test").searchParams.get("session_id");
        if (expectedSession !== owner) return response({}, 409);
        running = false;
        return response({ deleted: true, context_deleted: true });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });
    const first = renderHook(
      ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
      { initialProps: { sessionId: null as string | null } },
    );
    const stale = renderHook(() => useDraftWorkspace(null));
    await waitFor(() => expect(stale.result.current.terminals).toHaveLength(1));
    await act(async () => {
      await first.result.current.adopt("conv_1");
    });
    first.rerender({ sessionId: "conv_1" });
    expect(stale.result.current.context?.session_id).toBeNull();
    await act(async () => {
      await stale.result.current.deleteTerminal("shared_shell");
    });
    expect(running).toBe(true);
    expect(stale.result.current.context).toBeNull();
    expect(stale.result.current.error).toBeNull();
    expect(first.result.current.terminals[0]?.id).toBe("shared_shell");
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)[0].sessionId).toBe("conv_1");
    await act(async () => {
      await first.result.current.deleteTerminal("shared_shell");
    });
    expect(running).toBe(false);
    expect(first.result.current.context).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host_1/workspace-contexts/shared/resources/terminals/shared_shell?session_id=conv_1",
      { method: "DELETE" },
    );
  });

  it("cleans a retried shell when its initiating view changes before creation completes", async () => {
    restoreContexts(storedContext("shared"));
    let adopted = false;
    let resolveCreate!: (value: Response) => void;
    const creation = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/shared/heartbeat"))
        return response(context("shared", "/repo", adopted ? "conv_1" : null));
      if (url === "/v1/hosts/host_1/workspace-contexts") return response(context("fresh", "/repo"));
      if (url.endsWith("/resources/terminals")) {
        if (init?.method !== "POST") return response({ object: "list", data: [] });
        return url.includes("/shared/") ? response({}, 409) : creation;
      }
      if (url.endsWith("/fresh/resources/terminals/late") && init?.method === "DELETE")
        return response({ deleted: true });
      if (url.endsWith("/fresh") && init?.method === "DELETE") return response({ deleted: true });
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });
    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
      { initialProps: { sessionId: null as string | null } },
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    adopted = true;
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.createTerminal().catch((error: unknown) => error);
    });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_1/workspace-contexts/fresh/resources/terminals",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    rerender({ sessionId: "conv_other" });
    await act(async () => {
      resolveCreate(response(terminal("late")));
      expect(await pending).toEqual(new Error("Draft workspace selection changed"));
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host_1/workspace-contexts/fresh/resources/terminals/late",
      { method: "DELETE" },
    );
    expect(fetchMock).not.toHaveBeenCalledWith("/v1/hosts/host_1/workspace-contexts/shared", {
      method: "DELETE",
    });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual([
      expect.objectContaining({ id: "shared", sessionId: "conv_1" }),
    ]);
  });

  it("reconciles a heartbeat after shared storage records another tab's adoption", async () => {
    vi.useFakeTimers();
    const persisted = {
      id: "shared",
      workspace: "/repo",
      hostId: "host_1",
      leaseSeconds: 600,
      sessionId: null as string | null,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify([persisted]));
    let sharedSession: string | null = null;
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/heartbeat")) return response(context("shared", "/repo", sharedSession));
      if (url.endsWith("/resources/terminals"))
        return response({ object: "list", data: [terminal()] });
      throw new Error(`unexpected fetch: ${url}`);
    });
    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {});
    expect(result.current.context?.session_id).toBeNull();
    sharedSession = "conv_1";
    localStorage.setItem(STORAGE_KEY, JSON.stringify([{ ...persisted, sessionId: sharedSession }]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(result.current.context).toBeNull();
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)[0].sessionId).toBe("conv_1");
  });

  it("does not overwrite shared adoption with an older heartbeat response", async () => {
    const persisted = {
      id: "shared",
      workspace: "/repo",
      hostId: "host_1",
      leaseSeconds: 600,
      sessionId: null,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify([persisted]));
    let resolveHeartbeat!: (value: Response) => void;
    const heartbeat = new Promise<Response>((resolve) => {
      resolveHeartbeat = resolve;
    });
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/heartbeat")) return heartbeat;
      if (url.endsWith("/resources/terminals"))
        return response({ object: "list", data: [terminal()] });
      throw new Error(`unexpected fetch: ${url}`);
    });
    renderHook(() => useDraftWorkspace(null));
    localStorage.setItem(STORAGE_KEY, JSON.stringify([{ ...persisted, sessionId: "conv_1" }]));
    await act(async () => {
      resolveHeartbeat(response(context("shared", "/repo")));
      await heartbeat;
    });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)[0].sessionId).toBe("conv_1");
  });

  it("reconciles stale draft cleanup after another restored tab adopts the shell", async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        { id: "shared", workspace: "/repo", hostId: "host_1", leaseSeconds: 600, sessionId: null },
      ]),
    );
    let adoptedSession: string | null = null;
    let shellRunning = true;
    let resolveDelete!: (value: Response) => void;
    const deletion = new Promise<Response>((resolve) => {
      resolveDelete = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/shared/heartbeat")) {
        return response(context("shared", "/repo", adoptedSession));
      }
      if (url.endsWith("/shared/handoff")) {
        adoptedSession = "conv_1";
        return response(context("shared", "/repo", adoptedSession));
      }
      if (url.endsWith("/shared/resources/terminals")) {
        return response({ object: "list", data: shellRunning ? [terminal("shared_shell")] : [] });
      }
      if (url.endsWith("/shared") && init?.method === "DELETE") {
        if (adoptedSession === null) shellRunning = false;
        return deletion;
      }
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        return response(context("new_draft", "/other"));
      }
      if (url.endsWith("/new_draft/resources/terminals")) {
        return response({ object: "list", data: [] });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });
    const first = renderHook(
      ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
      { initialProps: { sessionId: null as string | null } },
    );
    const stale = renderHook(() => useDraftWorkspace(null));
    await waitFor(() => expect(first.result.current.terminals).toHaveLength(1));
    await waitFor(() => expect(stale.result.current.terminals).toHaveLength(1));
    await act(async () => {
      await first.result.current.adopt("conv_1");
    });
    first.rerender({ sessionId: "conv_1" });
    expect(stale.result.current.context?.session_id).toBeNull();

    let discard!: Promise<void>;
    act(() => {
      discard = stale.result.current.discard();
    });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual([
      expect.objectContaining({ id: "shared", sessionId: "conv_1" }),
    ]);
    await act(async () => {
      resolveDelete(response({ ...context("shared", "/repo", "conv_1"), deleted: false }));
      await discard;
      await stale.result.current.ensureContext("host_1", "/other");
    });

    expect(shellRunning).toBe(true);
    expect(first.result.current.terminals[0]?.id).toBe("shared_shell");
    expect(stale.result.current.context?.workspace).toBe("/other");
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "shared", sessionId: "conv_1" }),
        expect.objectContaining({ id: "new_draft", sessionId: null }),
      ]),
    );
  });

  it("forgets an empty context retired by handoff without failing Start", async () => {
    restoreContexts(storedContext("empty"));
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/heartbeat")) return response(context("empty", "/repo"));
      if (url.endsWith("/resources/terminals")) return response({ object: "list", data: [] });
      if (url.endsWith("/handoff")) {
        return response({ ...context("empty", "/repo", "conv_1"), context_deleted: true });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    const { result } = renderHook(() => useDraftWorkspace(null));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    await act(async () => {
      expect(await result.current.adopt("conv_1")).toBeNull();
    });
    expect(result.current.context).toBeNull();
    expect(result.current.error).toBeNull();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("starts idle when there is no selected context", () => {
    const { result } = renderHook(() => useDraftWorkspace(null));

    expect(result.current.context).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });

  it("treats a restored context inventory as unknown until its first successful list", async () => {
    restoreContexts(storedContext("context_restored"));
    let resolveList: ((value: Response) => void) | undefined;
    const listResponse = new Promise<Response>((resolve) => {
      resolveList = resolve;
    });
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_restored/heartbeat") {
        return response(context("context_restored", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_restored/resources/terminals") {
        return listResponse;
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    expect(result.current.context).toMatchObject({ id: "context_restored" });
    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      resolveList?.(response({ object: "list", data: [] }));
      await listResponse;
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.terminals).toEqual([]);
  });

  it("creates lazily, polls terminals, and keeps an adopted context available by session", async () => {
    let createdTerminal = false;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toEqual({ workspace: "/repo/../repo" });
        return response(context("context_1", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_1/resources/terminals") {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          expect(body.terminal).toBe("bash");
          expect(body.session_key).toMatch(/^draft-/);
          createdTerminal = true;
          return response(terminal());
        }
        return response({ object: "list", data: createdTerminal ? [terminal()] : [] });
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_1/handoff") {
        expect(JSON.parse(String(init?.body))).toEqual({ session_id: "conv_1" });
        return response(context("context_1", "/repo", "conv_1"));
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
      { initialProps: { sessionId: null as string | null } },
    );
    expect(result.current.context).toBeNull();

    await act(async () => {
      await result.current.ensureContext("host_1", "/repo/../repo");
      await result.current.createTerminal();
    });
    expect(result.current.context).toMatchObject({
      id: "context_1",
      hostId: "host_1",
      workspace: "/repo",
      session_id: null,
    });
    expect(localStorage.getItem(STORAGE_KEY)).toContain('"workspace":"/repo"');

    await act(async () => {
      await result.current.ensureContext("host_1", "/repo/../repo");
    });
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) =>
          url === "/v1/hosts/host_1/workspace-contexts" &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toHaveLength(1);

    expect(result.current.terminals).toEqual([
      expect.objectContaining({ id: "terminal_bash_draft", name: "bash", running: true }),
    ]);

    await act(async () => {
      await result.current.adopt("conv_1");
    });
    expect(result.current.context).toBeNull();

    rerender({ sessionId: "conv_1" });
    expect(result.current.context).toMatchObject({ id: "context_1", session_id: "conv_1" });
    expect(result.current.terminals).toHaveLength(1);
  });

  it("does not erase a newly created terminal with an older inventory response", async () => {
    let resolveList: ((value: Response) => void) | undefined;
    const listResponse = new Promise<Response>((resolve) => {
      resolveList = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        return response(context("context_inventory", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_inventory/resources/terminals") {
        return init?.method === "POST" ? response(terminal("terminal_new")) : listResponse;
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {
      await result.current.ensureContext("host_1", "/repo");
    });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_1/workspace-contexts/context_inventory/resources/terminals",
      ),
    );

    await act(async () => {
      await result.current.createTerminal();
    });
    expect(result.current.terminals).toEqual([expect.objectContaining({ id: "terminal_new" })]);

    await act(async () => {
      resolveList?.(response({ object: "list", data: [] }));
      await listResponse;
    });
    expect(result.current.terminals).toEqual([expect.objectContaining({ id: "terminal_new" })]);
  });

  it("does not resurrect a deleted terminal with an older inventory response", async () => {
    let inventoryRequests = 0;
    let resolveStaleList: ((value: Response) => void) | undefined;
    const staleListResponse = new Promise<Response>((resolve) => {
      resolveStaleList = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        return response(context("context_delete", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_delete/resources/terminals") {
        inventoryRequests += 1;
        return inventoryRequests === 1
          ? response({ object: "list", data: [terminal("terminal_old")] })
          : staleListResponse;
      }
      if (
        url ===
          "/v1/hosts/host_1/workspace-contexts/context_delete/resources/terminals/terminal_old" &&
        init?.method === "DELETE"
      ) {
        return response({ id: "terminal_old", deleted: true });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {
      await result.current.ensureContext("host_1", "/repo");
    });
    await waitFor(() =>
      expect(result.current.terminals).toEqual([expect.objectContaining({ id: "terminal_old" })]),
    );

    let staleRefresh: Promise<unknown> | undefined;
    act(() => {
      staleRefresh = result.current.refreshTerminals();
    });
    await waitFor(() => expect(inventoryRequests).toBe(2));
    await act(async () => {
      await result.current.deleteTerminal("terminal_old");
    });
    expect(result.current.terminals).toEqual([]);

    await act(async () => {
      resolveStaleList?.(response({ object: "list", data: [terminal("terminal_old")] }));
      await staleRefresh;
    });
    expect(result.current.terminals).toEqual([]);
  });

  it("forgets an adopted context when its final terminal deletion retires it", async () => {
    restoreContexts(storedContext("context_final", "/repo", "conv_1"));
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_final/heartbeat") {
        return response(context("context_final", "/repo", "conv_1"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_final/resources/terminals") {
        return response({ object: "list", data: [terminal("terminal_final")] });
      }
      if (
        url ===
          "/v1/hosts/host_1/workspace-contexts/context_final/resources/terminals/terminal_final?session_id=conv_1" &&
        init?.method === "DELETE"
      ) {
        return response({ id: "terminal_final", deleted: true, context_deleted: true });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace("conv_1"));
    await waitFor(() =>
      expect(result.current.terminals).toEqual([expect.objectContaining({ id: "terminal_final" })]),
    );

    await act(async () => {
      await result.current.deleteTerminal("terminal_final");
    });
    expect(result.current.context).toBeNull();
    expect(result.current.terminals).toEqual([]);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("forgets an adopted context when another viewer removes its final terminal", async () => {
    restoreContexts(storedContext("context_remote_final", "/repo", "conv_1"));
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_remote_final/heartbeat") {
        return response(context("context_remote_final", "/repo", "conv_1"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_remote_final/resources/terminals") {
        return response({}, 404);
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace("conv_1"));
    await waitFor(() => expect(result.current.context).toBeNull());
    expect(result.current.terminals).toEqual([]);
    expect(result.current.error).toBeNull();
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("accepts a slow inventory response while a newer poll is still in flight", async () => {
    const resolveLists: ((value: Response) => void)[] = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        return response(context("context_slow", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_slow/resources/terminals") {
        return new Promise<Response>((resolve) => {
          resolveLists.push(resolve);
        });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {
      await result.current.ensureContext("host_1", "/repo");
    });
    await waitFor(() => expect(resolveLists).toHaveLength(1));

    let newerPoll: Promise<unknown> | undefined;
    act(() => {
      newerPoll = result.current.refreshTerminals();
    });
    await waitFor(() => expect(resolveLists).toHaveLength(2));

    await act(async () => {
      resolveLists[0](response({ object: "list", data: [terminal("terminal_slow")] }));
    });
    expect(result.current.terminals).toEqual([expect.objectContaining({ id: "terminal_slow" })]);

    await act(async () => {
      resolveLists[1](response({ object: "list", data: [terminal("terminal_slow")] }));
      await newerPoll;
    });
  });

  it("hydrates every retained context, heartbeats all, and lists only the selected session", async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        {
          id: "context_adopted",
          workspace: "/adopted",
          hostId: "host_1",
          leaseSeconds: 600,
          sessionId: "conv_1",
        },
        {
          id: "context_draft",
          workspace: "/draft",
          hostId: "host_2",
          leaseSeconds: 600,
          sessionId: null,
        },
      ]),
    );
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_adopted/heartbeat") {
        return response(context("context_adopted", "/adopted", "conv_1"));
      }
      if (url === "/v1/hosts/host_2/workspace-contexts/context_draft/heartbeat") {
        return response(context("context_draft", "/draft"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_adopted/resources/terminals") {
        return response({ object: "list", data: [terminal("terminal_adopted")] });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace("conv_1"));
    expect(result.current.context).toMatchObject({ id: "context_adopted", hostId: "host_1" });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_1/workspace-contexts/context_adopted/heartbeat",
        { method: "POST" },
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_2/workspace-contexts/context_draft/heartbeat",
        { method: "POST" },
      );
      expect(result.current.terminals).toEqual([
        expect.objectContaining({ id: "terminal_adopted" }),
      ]);
    });
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("context_draft/resources/terminals"),
      ),
    ).toBe(false);
  });

  it("quarantines an old context when cleanup fails and still activates the new target", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        const workspace = (JSON.parse(String(init.body)) as { workspace: string }).workspace;
        return workspace === "/old"
          ? response(context("context_old", "/old"))
          : response(context("context_new", "/new"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_old" && init?.method === "DELETE") {
        return response({}, 500);
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_new/resources/terminals") {
        return response({ object: "list", data: [] });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {
      await result.current.ensureContext("host_1", "/old");
      await result.current.ensureContext("host_1", "/new");
    });

    expect(result.current.context).toMatchObject({ id: "context_new", workspace: "/new" });
    expect(localStorage.getItem(STORAGE_KEY)).toContain("context_new");
    expect(localStorage.getItem(STORAGE_KEY)).not.toContain("context_old");
  });

  it("cleans up a superseded context create before activating the latest target", async () => {
    let resolveOldCreate: ((value: Response) => void) | undefined;
    const oldCreateResponse = new Promise<Response>((resolve) => {
      resolveOldCreate = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        const workspace = (JSON.parse(String(init.body)) as { workspace: string }).workspace;
        return workspace === "/old" ? oldCreateResponse : response(context("context_new", "/new"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_old" && init?.method === "DELETE") {
        return response({ id: "context_old", deleted: true });
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_new/resources/terminals") {
        return response({ object: "list", data: [] });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    const oldOutcome = result.current
      .ensureContext("host_1", "/old")
      .catch((cause: unknown) => cause);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_1/workspace-contexts",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const newContext = result.current.ensureContext("host_1", "/new");

    await act(async () => {
      resolveOldCreate?.(response(context("context_old", "/old")));
      await oldCreateResponse;
    });
    await expect(oldOutcome).resolves.toMatchObject({
      message: "Draft workspace selection changed",
    });
    await expect(newContext).resolves.toMatchObject({ id: "context_new" });
    await waitFor(() => expect(result.current.context).toMatchObject({ id: "context_new" }));
    expect(fetchMock).toHaveBeenCalledWith("/v1/hosts/host_1/workspace-contexts/context_old", {
      method: "DELETE",
    });
  });

  it("keeps a pending terminal create busy and cleans it up after the context is discarded", async () => {
    let resolveCreate: ((value: Response) => void) | undefined;
    const createResponse = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts" && init?.method === "POST") {
        return response(context("context_pending", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_pending/resources/terminals") {
        if (init?.method === "POST") return createResponse;
        return response({ object: "list", data: [] });
      }
      if (
        url ===
          "/v1/hosts/host_1/workspace-contexts/context_pending/resources/terminals/terminal_late" &&
        init?.method === "DELETE"
      ) {
        return response({ id: "terminal_late", deleted: true });
      }
      if (
        url === "/v1/hosts/host_1/workspace-contexts/context_pending" &&
        init?.method === "DELETE"
      ) {
        return response({ id: "context_pending", deleted: true });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    let captured: DraftWorkspaceContext | undefined;
    await act(async () => {
      captured = await result.current.ensureContext("host_1", "/repo");
    });

    let createOutcome: Promise<unknown> | undefined;
    act(() => {
      createOutcome = result.current.createTerminal(captured).catch((cause: unknown) => cause);
    });
    expect(result.current.isLoading).toBe(true);

    await act(async () => {
      await result.current.discard(captured);
    });
    expect(result.current.context).toBeNull();

    await act(async () => {
      resolveCreate?.(response(terminal("terminal_late")));
      await createResponse;
    });
    await expect(createOutcome).resolves.toMatchObject({
      message: "Draft workspace selection changed",
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/hosts/host_1/workspace-contexts/context_pending/resources/terminals/terminal_late",
      { method: "DELETE" },
    );
    expect(result.current.terminals).toEqual([]);
  });

  it("does not resurrect a discarded context from a late heartbeat", async () => {
    restoreContexts(storedContext("context_heartbeat"));
    let resolveHeartbeat: ((value: Response) => void) | undefined;
    const heartbeatResponse = new Promise<Response>((resolve) => {
      resolveHeartbeat = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_heartbeat/heartbeat") {
        return heartbeatResponse;
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_heartbeat/resources/terminals") {
        return response({ object: "list", data: [] });
      }
      if (
        url === "/v1/hosts/host_1/workspace-contexts/context_heartbeat" &&
        init?.method === "DELETE"
      ) {
        return response({ id: "context_heartbeat", deleted: true });
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    const captured = result.current.context;
    expect(captured).not.toBeNull();
    await act(async () => {
      await result.current.discard(captured);
    });
    expect(result.current.context).toBeNull();

    await act(async () => {
      resolveHeartbeat?.(response(context("context_heartbeat", "/repo")));
      await heartbeatResponse;
    });
    await waitFor(() => expect(result.current.context).toBeNull());
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("does not undo a successful handoff with a late pre-handoff heartbeat", async () => {
    restoreContexts(storedContext("context_handoff"));
    let resolveHeartbeat: ((value: Response) => void) | undefined;
    const heartbeatResponse = new Promise<Response>((resolve) => {
      resolveHeartbeat = resolve;
    });
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts/context_handoff/heartbeat") {
        return heartbeatResponse;
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_handoff/resources/terminals") {
        return response({ object: "list", data: [] });
      }
      if (
        url === "/v1/hosts/host_1/workspace-contexts/context_handoff/handoff" &&
        init?.method === "POST"
      ) {
        return response(context("context_handoff", "/repo", "conv_1"));
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) => useDraftWorkspace(sessionId),
      { initialProps: { sessionId: null as string | null } },
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/hosts/host_1/workspace-contexts/context_handoff/heartbeat",
        { method: "POST" },
      ),
    );

    await act(async () => {
      await result.current.adopt("conv_1");
    });
    rerender({ sessionId: "conv_1" });
    expect(result.current.context).toMatchObject({ id: "context_handoff", session_id: "conv_1" });

    await act(async () => {
      resolveHeartbeat?.(response(context("context_handoff", "/repo")));
      await heartbeatResponse;
    });
    await waitFor(() =>
      expect(result.current.context).toMatchObject({
        id: "context_handoff",
        session_id: "conv_1",
      }),
    );
    expect(localStorage.getItem(STORAGE_KEY)).toContain('"sessionId":"conv_1"');
  });

  it("retains the draft and its terminals when handoff fails", async () => {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/v1/hosts/host_1/workspace-contexts") {
        return response(context("context_1", "/repo"));
      }
      if (url === "/v1/hosts/host_1/workspace-contexts/context_1/resources/terminals") {
        return response({ object: "list", data: [] });
      }
      if (
        url === "/v1/hosts/host_1/workspace-contexts/context_1/handoff" &&
        init?.method === "POST"
      ) {
        return response({}, 500);
      }
      throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
    });

    const { result } = renderHook(() => useDraftWorkspace(null));
    await act(async () => {
      await result.current.ensureContext("host_1", "/repo");
    });

    await expect(result.current.adopt("conv_1")).rejects.toThrow(
      "draft workspace handoff failed: 500 Failed",
    );
    expect(result.current.context).toMatchObject({ id: "context_1", session_id: null });
    expect(localStorage.getItem(STORAGE_KEY)).toContain("context_1");
  });
});
