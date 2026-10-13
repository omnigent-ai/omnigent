import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  StaticSandboxesApiError,
  listAvailableSandboxes,
  listStaticSandboxes,
  removeStaticSandbox,
  saveStaticSandbox,
  wakeStaticSandbox,
  wakeSessionHost,
} from "./staticSandboxesApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "",
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

describe("staticSandboxesApi", () => {
  it("lists saved entries, falling back to the sandbox id as the label", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        static_sandboxes: [
          {
            provider: "remote",
            sandbox_id: "foo",
            label: "Checkout",
            status: "running",
            repo: "o/r",
          },
          {
            provider: "other",
            sandbox_id: "bar",
            label: "",
            status: "transitioning",
            last_activity_at: null,
          },
        ],
      }),
    );
    const entries = await listStaticSandboxes();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/static-sandboxes");
    expect(entries).toEqual([
      {
        provider: "remote",
        id: "foo",
        label: "Checkout",
        status: "running",
        repo: "o/r",
        lastActivityAt: null,
      },
      {
        provider: "other",
        id: "bar",
        label: "bar",
        status: "paused",
        repo: "",
        lastActivityAt: null,
      },
    ]);
  });

  it("lists the picker's sandboxes from /available", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({
        available_sandboxes: [
          { provider: "remote", sandbox_id: "foo", status: "paused", repo: "" },
        ],
      }),
    );
    const vms = await listAvailableSandboxes();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/static-sandboxes/available");
    expect(vms[0]).toMatchObject({ provider: "remote", id: "foo", status: "paused" });
  });

  it("PUTs a label to add or rename", async () => {
    fetchMock.mockResolvedValue(
      mockResponse({ provider: "remote", sandbox_id: "foo", label: "New name", status: "running" }),
    );
    const saved = await saveStaticSandbox("remote", "foo", "New name");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/static-sandboxes/remote/foo");
    expect((init as RequestInit).method).toBe("PUT");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ label: "New name" });
    expect(saved.label).toBe("New name");
  });

  it("DELETEs an entry and treats a missing one as removed", async () => {
    fetchMock.mockResolvedValue(mockResponse({}, { ok: false, status: 404 }));
    await removeStaticSandbox("remote", "a/b");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/static-sandboxes/remote/a%2Fb");
    expect((init as RequestInit).method).toBe("DELETE");
  });

  it("wakes by provider and sandbox id, and by session", async () => {
    fetchMock.mockResolvedValue(mockResponse({ host_id: "h" }));
    await wakeStaticSandbox("remote", "foo");
    await wakeSessionHost("conv_1");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/v1/static-sandboxes/remote/foo/wake",
      "/v1/sessions/conv_1/wake",
    ]);
  });

  it("throws the server's message and status", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(
        { error: { message: "Static sandboxes are not available" } },
        { ok: false, status: 503 },
      ),
    );
    const err = await listStaticSandboxes().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(StaticSandboxesApiError);
    expect((err as StaticSandboxesApiError).status).toBe(503);
    expect((err as Error).message).toContain("not available");
  });
});
