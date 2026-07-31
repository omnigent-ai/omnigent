import { describe, expect, it, vi, beforeEach } from "vitest";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { importAgentFromGit, refreshAgent } = await import("./agentsApi");

function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

beforeEach(() => fetchMock.mockReset());

describe("importAgentFromGit", () => {
  it("POSTs url/ref/subpath/host_id and returns the agent", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "ag_1", name: "x", git_ref: "main" }));
    const out = await importAgentFromGit({
      gitUrl: "https://github.com/org/repo",
      gitRef: "main",
      hostId: "h_abc",
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/agents/import-git");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      git_url: "https://github.com/org/repo",
      git_ref: "main",
      git_subpath: null,
      host_id: "h_abc",
    });
    expect(out.id).toBe("ag_1");
  });

  it("includes host_id in the POST body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "ag_2", name: "y" }));
    await importAgentFromGit({ gitUrl: "https://github.com/org/repo2", hostId: "h_xyz" });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body.host_id).toBe("h_xyz");
  });

  it("throws the server message on 400", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: { message: "Not a valid git URL." } }, { ok: false, status: 400 }),
    );
    await expect(importAgentFromGit({ gitUrl: "file:///x", hostId: "h_1" })).rejects.toThrow(
      /valid git URL/,
    );
  });
});

describe("refreshAgent", () => {
  it("POSTs to the refresh endpoint", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "ag_1", version: 2 }));
    const out = await refreshAgent("ag_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/agents/ag_1/refresh");
    expect(init.method).toBe("POST");
    expect(out.version).toBe(2);
  });
});
