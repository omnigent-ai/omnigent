import { beforeEach, describe, expect, it, vi } from "vitest";
import { getAgentConfig, patchAgentConfig, installAgentBundle } from "./agentsApi";
import { authenticatedFetch } from "./identity";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const mockFetch = vi.mocked(authenticatedFetch);
const bundle = new File([new Uint8Array([0x1f, 0x8b])], "orion.tar.gz");

function response(body: unknown, status = 200, statusText = "OK"): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText,
    json: async () => {
      if (body === undefined) throw new SyntaxError("not json");
      return body;
    },
  } as unknown as Response;
}

describe("installAgentBundle", () => {
  beforeEach(() => mockFetch.mockReset());

  it("posts the bundle as multipart and returns the installed agent", async () => {
    mockFetch.mockResolvedValue(response({ id: "ag_1", name: "orion", version: 1 }));

    await expect(installAgentBundle(bundle)).resolves.toEqual({ id: "ag_1", name: "orion" });

    const [url, init] = mockFetch.mock.calls[0];
    expect(url).toBe("/v1/agents");
    expect(init?.method).toBe("POST");
    expect((init!.body as FormData).get("bundle")).toBeInstanceOf(File);
  });

  it("surfaces the server's structured rejection", async () => {
    mockFetch.mockResolvedValue(
      response({ error: { code: "conflict", message: "'polly' is a built-in agent" } }, 409),
    );
    await expect(installAgentBundle(bundle)).rejects.toThrow("'polly' is a built-in agent");
  });

  it("falls back to the status line for a non-JSON failure", async () => {
    mockFetch.mockResolvedValue(response(undefined, 502, "Bad Gateway"));
    await expect(installAgentBundle(bundle)).rejects.toThrow("502 Bad Gateway");
  });

  it("rejects a success response without an id and name", async () => {
    mockFetch.mockResolvedValue(response({ ok: true }));
    await expect(installAgentBundle(bundle)).rejects.toThrow(/Unexpected response/);
  });
});

describe("configuration API", () => {
  it("reads the exact revision and sends it with a minimal PATCH", async () => {
    const signal = new AbortController().signal;
    mockFetch.mockResolvedValue(
      new Response(JSON.stringify({ id: "a/b" }), { headers: { ETag: '"revision"' } }),
    );
    const loaded = await getAgentConfig("a/b", signal);
    expect(loaded.etag).toBe('"revision"');
    expect(mockFetch).toHaveBeenLastCalledWith("/v1/agents/a%2Fb/config", { signal });
    mockFetch.mockResolvedValue(new Response('{"id":"a/b"}', { headers: { ETag: '"new"' } }));
    await patchAgentConfig("a/b", loaded.etag!, { description: null });
    expect(mockFetch).toHaveBeenLastCalledWith("/v1/agents/a%2Fb", {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "If-Match": '"revision"' },
      body: '{"description":null}',
    });
  });
  it("preserves missing revision information and exposes conflicts", async () => {
    mockFetch.mockResolvedValue(new Response("{}"));
    expect((await getAgentConfig("a")).etag).toBeNull();
    mockFetch.mockResolvedValue(
      new Response('{"error":{"code":"precondition_failed","message":"Reload"}}', { status: 412 }),
    );
    await expect(patchAgentConfig("a", '"old"', {})).rejects.toMatchObject({
      status: 412,
      code: "precondition_failed",
    });
  });
});
