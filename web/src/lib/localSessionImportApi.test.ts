import { beforeEach, describe, expect, it, vi } from "vitest";

import { fetchHostLocalSessions, importHostLocalSession } from "./localSessionImportApi";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const { authenticatedFetch } = await import("@/lib/identity");
const mockFetch = vi.mocked(authenticatedFetch);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("fetchHostLocalSessions", () => {
  beforeEach(() => mockFetch.mockReset());

  it("requests the host's recent sessions and returns the list", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [
          {
            source: "claude",
            external_session_id: "abc",
            workspace: "/repo",
            title: "inspect TODO.md",
            item_count: 4,
            preview: [{ role: "user", text: "inspect TODO.md" }],
          },
        ],
      }),
    );

    const sessions = await fetchHostLocalSessions("host_a", "claude", 10);

    expect(sessions).toHaveLength(1);
    expect(sessions[0].external_session_id).toBe("abc");
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain("/v1/imports/local-sessions");
    expect(url).toContain("host_id=host_a");
    expect(url).toContain("source=claude");
    expect(url).toContain("limit=10");
  });

  it("throws the server's message when the host cannot be reached", async () => {
    mockFetch.mockResolvedValue(jsonResponse({ detail: "host is offline" }, 409));

    await expect(fetchHostLocalSessions("host_a", "claude")).rejects.toThrow("host is offline");
  });
});

describe("importHostLocalSession", () => {
  beforeEach(() => mockFetch.mockReset());

  it("posts the source and session id and returns the new session", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({ session_id: "conv_1", status: "imported", item_count: 4 }, 201),
    );

    const result = await importHostLocalSession("host_a", "codex", "thread-1");

    expect(result.session_id).toBe("conv_1");
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      host_id: "host_a",
      source: "codex",
      external_session_id: "thread-1",
    });
  });

  it("throws the server's message when the session was already imported", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({ error: { message: "already imported as conv_1" } }, 409),
    );

    await expect(importHostLocalSession("host_a", "claude", "abc")).rejects.toThrow(
      "already imported as conv_1",
    );
  });
});
