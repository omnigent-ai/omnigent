import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";

import { markHostKeyless, clearHostKeyless } from "@/lib/sessionHost";
import { landingStorageKey } from "@/lib/landingStorage";
import {
  buildDraftTerminalAttachPath,
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

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
});

afterEach(() => {
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

describe("useDraftWorkspace", () => {
  it("starts idle when there is no selected context", () => {
    const { result } = renderHook(() => useDraftWorkspace(null));

    expect(result.current.context).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });

  it("treats a restored context inventory as unknown until its first successful list", async () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        {
          id: "context_restored",
          workspace: "/repo",
          hostId: "host_1",
          leaseSeconds: 600,
          sessionId: null,
        },
      ]),
    );
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
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        {
          id: "context_heartbeat",
          workspace: "/repo",
          hostId: "host_1",
          leaseSeconds: 600,
          sessionId: null,
        },
      ]),
    );
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
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify([
        {
          id: "context_handoff",
          workspace: "/repo",
          hostId: "host_1",
          leaseSeconds: 600,
          sessionId: null,
        },
      ]),
    );
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
