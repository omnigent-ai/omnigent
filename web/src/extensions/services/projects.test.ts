import { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cachedProjectSummaries,
  createProjectSummary,
  listProjectSummaries,
  parseCreateProjectParams,
  projectSummary,
} from "./projects";

const { authenticatedFetchMock } = vi.hoisted(() => ({ authenticatedFetchMock: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));

beforeEach(() => authenticatedFetchMock.mockReset());

describe("projectSummary", () => {
  it("keeps only id, name, and icon and bounds the strings", () => {
    const summary = projectSummary({
      id: "proj_1",
      name: "n".repeat(300),
      user_id: "private@example.com",
      created_at: 1,
      config: { icon: "🔥".repeat(20) },
    });
    expect(Object.keys(summary).sort()).toEqual(["icon", "id", "name"]);
    expect(summary.name).toHaveLength(100);
    expect(summary.icon).toHaveLength(16);
    expect(projectSummary({ id: "proj_2", name: "Plain" }).icon).toBeNull();
  });
});

describe("cachedProjectSummaries", () => {
  it("reuses first-class sidebar projects and ignores legacy label folders", () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(
      ["projects"],
      [
        { id: "proj_1", name: "Alpha", icon: "🅰️" },
        { id: null, name: "Legacy", icon: null },
      ],
    );

    expect(cachedProjectSummaries(queryClient)).toEqual([
      { id: "proj_1", name: "Alpha", icon: "🅰️" },
    ]);
  });
});

describe("parseCreateProjectParams", () => {
  it("trims the name and rejects empty or oversized names", () => {
    expect(parseCreateProjectParams({ name: "  Alpha  " })).toBe("Alpha");
    for (const params of [null, [], {}, { name: 3 }, { name: "   " }, { name: "x".repeat(101) }]) {
      expect(() => parseCreateProjectParams(params)).toThrow(
        expect.objectContaining({ code: "InvalidParams" }),
      );
    }
  });
});

describe("project host requests", () => {
  it("lists projects through the projects API", async () => {
    authenticatedFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            { id: "p1", name: "Alpha", config: { icon: "🅰️" } },
            { id: "p2", name: "Beta" },
          ],
        }),
        { status: 200 },
      ),
    );
    await expect(listProjectSummaries()).resolves.toEqual([
      { id: "p1", name: "Alpha", icon: "🅰️" },
      { id: "p2", name: "Beta", icon: null },
    ]);
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/projects");
  });

  it("posts the trimmed name and surfaces the server's rejection", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ id: "p3", name: "Gamma", config: {} }), { status: 200 }),
    );
    await expect(createProjectSummary("Gamma")).resolves.toEqual({
      id: "p3",
      name: "Gamma",
      icon: null,
    });
    const [url, init] = authenticatedFetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/projects");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ name: "Gamma" });

    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { message: "name must not be empty" } }), {
        status: 400,
      }),
    );
    await expect(createProjectSummary("Delta")).rejects.toMatchObject({
      code: "HostError",
      message: "Project could not be created: name must not be empty",
    });
  });
});
