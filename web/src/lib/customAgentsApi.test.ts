import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ authenticatedFetch: vi.fn() }));

vi.mock("./identity", () => ({ authenticatedFetch: mocks.authenticatedFetch }));

import {
  createCustomAgent,
  deleteCustomAgent,
  getCustomAgent,
  importCustomAgent,
  listCustomAgents,
  updateCustomAgent,
  type CustomAgentDetail,
} from "./customAgentsApi";

const detail: CustomAgentDetail = {
  id: "ag_custom/one",
  name: "Reviewer",
  description: null,
  harness: "codex",
  model: null,
  version: 4,
  created_at: 1,
  updated_at: null,
  instructions: "Review carefully.",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => mocks.authenticatedFetch.mockReset());

describe("customAgentsApi", () => {
  it("paginates the custom catalog using the accumulated offset", async () => {
    const first = [detail, { ...detail, id: "ag_two", name: "Writer" }];
    const second = [{ ...detail, id: "ag_three", name: "Planner" }];
    mocks.authenticatedFetch
      .mockResolvedValueOnce(jsonResponse({ data: first, has_more: true }))
      .mockResolvedValueOnce(jsonResponse({ data: second, has_more: false }));

    await expect(listCustomAgents()).resolves.toEqual([...first, ...second]);
    expect(mocks.authenticatedFetch.mock.calls).toEqual([
      ["/v1/custom-agents?limit=100&offset=0"],
      ["/v1/custom-agents?limit=100&offset=2"],
    ]);
  });

  it("sends create, import, update, and delete requests with their required wire shapes", async () => {
    mocks.authenticatedFetch.mockImplementation(() => Promise.resolve(jsonResponse(detail)));
    const bundle = new File(["bundle"], "agent.tar.gz", { type: "application/gzip" });

    await expect(createCustomAgent(bundle)).resolves.toEqual(detail);
    const createOptions = mocks.authenticatedFetch.mock.calls[0][1] as RequestInit;
    expect(mocks.authenticatedFetch.mock.calls[0][0]).toBe("/v1/custom-agents");
    expect(createOptions.method).toBe("POST");
    expect(createOptions.body).toBeInstanceOf(FormData);
    expect((createOptions.body as FormData).get("bundle")).toBe(bundle);

    await importCustomAgent("sess_1");
    expect(mocks.authenticatedFetch).toHaveBeenNthCalledWith(2, "/v1/custom-agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_session_id: "sess_1" }),
    });

    const changes = {
      name: "Release reviewer",
      description: "Checks releases",
      instructions: null,
      version: 4,
    };
    await updateCustomAgent(detail.id, changes);
    expect(mocks.authenticatedFetch).toHaveBeenNthCalledWith(
      3,
      "/v1/custom-agents/ag_custom%2Fone",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      },
    );

    mocks.authenticatedFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(deleteCustomAgent(detail.id)).resolves.toBeUndefined();
    expect(mocks.authenticatedFetch).toHaveBeenNthCalledWith(
      4,
      "/v1/custom-agents/ag_custom%2Fone",
      { method: "DELETE" },
    );
  });

  it("surfaces server detail errors and falls back to the HTTP status", async () => {
    mocks.authenticatedFetch.mockResolvedValueOnce(
      jsonResponse({ detail: "Version conflict" }, 409),
    );
    await expect(getCustomAgent(detail.id)).rejects.toThrow("Version conflict");

    mocks.authenticatedFetch.mockResolvedValueOnce(
      new Response("upstream failed", { status: 502 }),
    );
    await expect(deleteCustomAgent(detail.id)).rejects.toThrow("Agent request failed (502)");
  });
});
