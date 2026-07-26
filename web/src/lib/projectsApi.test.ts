// Unit tests for `projectsApi.ts` — the `/v1/projects` first-class CRUD
// client. Happy-path requests with a mocked `fetch`, plus error-path coverage
// that surfaces the server's structured `{error: {message}}` shape.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createProject, deleteProject, listProjects, renameProject } from "./projectsApi";

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
