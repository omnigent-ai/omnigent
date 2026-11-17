import { afterEach, expect, it, vi } from "vitest";
import { setMcpServerEnabled } from "./mcpServerEnabledApi";

afterEach(() => vi.unstubAllGlobals());

it.each([
  [{ detail: "Cannot update disabled MCP servers: io" }, "Cannot update disabled MCP servers: io"],
  [{ error: { code: "conflict", message: "host is offline" } }, "host is offline"],
  [{}, "502 Bad Gateway"],
])("preserves server error details or the status fallback", async (body, message) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 502,
        statusText: "Bad Gateway",
      }),
    ),
  );
  await expect(
    setMcpServerEnabled("host", { harness: "claude", server: "docs", enabled: false }),
  ).rejects.toMatchObject({ message, status: 502 });
});
