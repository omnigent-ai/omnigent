// Unit tests for `projectsApi.ts` — the `/v1/projects` first-class CRUD
// client. Happy-path requests with a mocked `fetch`, plus error-path coverage
// that surfaces the server's structured `{error: {message}}` shape.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createProject,
  deleteProject,
  getProject,
  listProjects,
  renameProject,
  updateProjectConfig,
} from "./projectsApi";

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

describe("listProjects", () => {
  it("GETs /v1/projects and returns the data array", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ object: "list", data: [{ id: "p_1", name: "A" }] }),
    );
    const result = await listProjects();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects");
    expect(result).toEqual([{ id: "p_1", name: "A" }]);
  });
});

describe("createProject", () => {
  it("shares an in-flight creation with dependent filing and releases it after failure", async () => {
    let reject!: (error: Error) => void;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((_resolve, fail) => {
        reject = fail;
      }),
    );
    const first = createProject("Pending", { icon: "🔥" });
    const dependent = createProject("Pending");
    expect(dependent).toBe(first);
    const settled = Promise.allSettled([first, dependent]);
    reject(new Error("offline"));
    expect((await settled).map((item) => item.status)).toEqual(["rejected", "rejected"]);
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "real", name: "Pending" }));
    expect(await createProject("Pending")).toMatchObject({ id: "real" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([undefined, { icon: "📁" }])(
    "rejects conflicting config instead of silently sharing %j",
    async (config) => {
      let finish!: (response: Response) => void;
      fetchMock.mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
      );
      const first = createProject("Pending", config);
      await expect(createProject("Pending", { icon: "🔥" })).rejects.toThrow("different settings");
      expect(fetchMock).toHaveBeenCalledTimes(1);
      finish(mockResponse({ id: "real", name: "Pending", config }));
      expect(await first).toMatchObject({ id: "real", config });
    },
  );

  it.each([
    [
      { host_id: "host", agent_id: "agent" },
      { agent_id: "agent", host_id: "host" },
    ],
    [undefined, {}],
    [{}, undefined],
    [{ icon: "🔥" }, { icon: undefined }],
  ])(
    "shares compatible configs regardless of key order: %j / %j",
    async (firstConfig, nextConfig) => {
      let finish!: (response: Response) => void;
      fetchMock.mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
      );
      const first = createProject("Pending", firstConfig);
      expect(createProject("Pending", nextConfig)).toBe(first);
      finish(mockResponse({ id: "real", name: "Pending", config: firstConfig }));
      await first;
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );

  it("POSTs the name and returns the project", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    const result = await createProject("New");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ name: "New" });
    expect(result.id).toBe("p_1");
  });

  it("surfaces the server error message on a duplicate name (409)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { message: "A project named 'New' already exists" } },
        {
          ok: false,
          status: 409,
        },
      ),
    );
    await expect(createProject("New")).rejects.toThrow("already exists");
  });

  it("includes config in the body when provided", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "New" }));
    await createProject("New", { host_id: "h1", agent_id: "ag_1" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      name: "New",
      config: { host_id: "h1", agent_id: "ag_1" },
    });
  });
});

describe("getProject", () => {
  it("preserves the HTTP status so callers can distinguish deleted projects from outages", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "not_found", message: "Project not found" } },
        { ok: false, status: 404 },
      ),
    );
    await expect(getProject("deleted")).rejects.toMatchObject({
      status: 404,
      code: "not_found",
      message: "Project not found",
    });
  });

  it("GETs /v1/projects/{id} and returns the project with config", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p_1", name: "A", config: { workspace: "/w" } }),
    );
    const result = await getProject("p_1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/projects/p_1");
    expect(result.config).toEqual({ workspace: "/w" });
  });
});

describe("updateProjectConfig", () => {
  it("PATCHes only the config field (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ id: "p a", name: "A", config: { agent_id: "ag_1" } }),
    );
    await updateProjectConfig("p a", { agent_id: "ag_1" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ config: { agent_id: "ag_1" } });
  });

  it("sends config:{} to clear stored defaults", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p_1", name: "A", config: {} }));
    await updateProjectConfig("p_1", {});
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ config: {} });
  });
});

describe("renameProject", () => {
  it("PATCHes /v1/projects/{id} with the new name (url-encoded id)", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ id: "p a", name: "Renamed" }));
    await renameProject("p a", "Renamed");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p%20a");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ name: "Renamed" });
  });
});

describe("deleteProject", () => {
  it("DELETEs /v1/projects/{id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true }));
    await deleteProject("p_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects/p_1");
    expect(init.method).toBe("DELETE");
  });

  it("throws on non-2xx", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 404 }));
    await expect(deleteProject("missing")).rejects.toThrow();
  });
});
