import { type IncomingMessage, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";
import type { BuildingService } from "./building.ts";
import { buildUpstreamUpgrade } from "./dictationProxy.ts";
import { createOfficeHandler, isAllowedHost, isAllowedOrigin } from "./http.ts";
import type { OmnigentClient } from "./omnigentClient.ts";
import type { WorkflowService } from "./workflow.ts";

const PORTS = [6780, 6781];

describe("isAllowedHost", () => {
  it.each(["127.0.0.1:6780", "localhost:6781", "[::1]:6780", "localhost"])("allows loopback host %s", (host) => {
    expect(isAllowedHost(host, PORTS)).toBe(true);
  });

  it.each(["evil.example:6780", "127.0.0.1:9999", "192.168.1.2:6780", undefined])("rejects %s", (host) => {
    expect(isAllowedHost(host, PORTS)).toBe(false);
  });
});

describe("isAllowedOrigin", () => {
  it("allows only the office's own loopback origins", () => {
    expect(isAllowedOrigin("http://127.0.0.1:6781", PORTS)).toBe(true);
    expect(isAllowedOrigin("http://localhost:6780", PORTS)).toBe(true);
    expect(isAllowedOrigin("https://evil.example", PORTS)).toBe(false);
    expect(isAllowedOrigin("http://localhost:3000", PORTS)).toBe(false);
    expect(isAllowedOrigin("null", PORTS)).toBe(false);
    expect(isAllowedOrigin(undefined, PORTS)).toBe(false);
  });
});

describe("office MCP endpoint", () => {
  const caller = { link: { role: "pm" }, branch: { id: "branch_1" } };
  const workflow = {
    callerForToken: (token: string) => (token === "good" ? caller : null),
    boardSnapshot: () => ({ ok: true }),
  } as unknown as WorkflowService;
  const building = { invalidate: () => undefined } as unknown as BuildingService;

  async function post(headers: Record<string, string>, body: unknown) {
    const ports: number[] = [];
    const handler = createOfficeHandler({ building, workflow, client: {} as OmnigentClient, allowedPorts: ports, staticDir: null });
    const server = createServer((request, response) => void handler(request, response));
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    ports.push(port);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/office/mcp`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...headers },
        body: JSON.stringify(body),
      });
      return { status: response.status, body: await response.text() };
    } finally {
      server.close();
    }
  }

  it("requires a live role token and refuses browser origins", async () => {
    const call = { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "office_board_get" } };
    expect((await post({}, call)).status).toBe(401);
    expect((await post({ Authorization: "Bearer bad" }, call)).status).toBe(401);
    expect((await post({ Authorization: "Bearer good", Origin: "http://127.0.0.1" }, call)).status).toBe(403);
    const ok = await post({ Authorization: "Bearer good" }, call);
    expect(ok.status).toBe(200);
    expect(JSON.parse(ok.body).result.isError).toBe(false);
  });
});

describe("room creation", () => {
  it("refuses an SDLC room with no host or runner before creating the project", async () => {
    let created = 0;
    const building = { createRoom: async () => ((created += 1), { projectId: "p" }), invalidate: () => undefined } as unknown as BuildingService;
    const ports: number[] = [];
    const handler = createOfficeHandler({ building, workflow: {} as WorkflowService, client: {} as OmnigentClient, allowedPorts: ports, staticDir: null });
    const server = createServer((request, response) => void handler(request, response));
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    ports.push(port);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/office/api/rooms`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: `http://127.0.0.1:${port}` },
        body: JSON.stringify({ name: "Shop", theme: { runtime: { harness: "claude-sdk" }, gates: {} } }),
      });
      expect(response.status).toBe(400);
      expect(created).toBe(0);
    } finally {
      server.close();
    }
  });
});

describe("buildUpstreamUpgrade", () => {
  it("forwards the WebSocket handshake with server-side credentials and Omnigent's own origin", () => {
    const request = {
      headers: { "sec-websocket-key": "abc==", "sec-websocket-version": "13", cookie: "browser-cookie" },
    } as unknown as IncomingMessage;
    const upgrade = buildUpstreamUpgrade(request, new URL("http://127.0.0.1:6767"), "/v1/dictation/stream", {
      Authorization: "Bearer token",
    });
    expect(upgrade.startsWith("GET /v1/dictation/stream HTTP/1.1\r\nHost: 127.0.0.1:6767\r\n")).toBe(true);
    expect(upgrade).toContain("Origin: http://127.0.0.1:6767\r\n");
    expect(upgrade).toContain("sec-websocket-key: abc==\r\n");
    expect(upgrade).toContain("Authorization: Bearer token\r\n");
    expect(upgrade).not.toContain("browser-cookie");
    expect(upgrade.endsWith("\r\n\r\n")).toBe(true);
  });
});
