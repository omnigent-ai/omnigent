import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { isExtensionPayloadWithinBudget } from "../rpc/validation";
import { ExtensionHostServiceError } from "./errors";
import {
  listSessionPage,
  parseSessionPageRequest,
  SessionReadLimiter,
  projectSessionPage,
  sessionListQuery,
} from "./sessions";

import { markConversationUnread, resetReadStateForTests } from "@/hooks/useUnseenConversations";
import { clearOptimisticTitles, recordOptimisticTitle } from "@/lib/optimisticTitles";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

afterEach(() => {
  resetReadStateForTests();
  clearOptimisticTitles();
});

const wireRow = {
  id: "conv_1",
  title: "Session one",
  status: "running",
  workspace: "/workspace/project",
  created_at: 10.5,
  updated_at: 20.75,
  owner: "private@example.com",
  permission_level: 4,
  labels: { secret: "value" },
  runner_id: "runner-secret",
  host_id: "host-secret",
  external_session_id: "external-secret",
  comments_count: 3,
  viewer_unread: true,
  search_snippet: "private text",
  project_id: "project-secret",
  archived: false,
};

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());
afterEach(() => vi.useRealTimers());

describe("SessionReadLimiter", () => {
  it("serializes reads and drops queued work after cancellation", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const limiter = new SessionReadLimiter();
    await limiter.run(new AbortController().signal, async () => "first");
    const operation = vi.fn(async () => "second");
    const controller = new AbortController();
    const queued = limiter.run(controller.signal, operation);
    await Promise.resolve();
    expect(operation).not.toHaveBeenCalled();
    controller.abort();
    vi.advanceTimersByTime(100);

    await expect(queued).rejects.toMatchObject({ name: "AbortError" });
    expect(operation).not.toHaveBeenCalled();
  });
});

describe("session page request", () => {
  it("builds a fixed top-level non-archived query", () => {
    const request = parseSessionPageRequest({ after: "conv a", limit: 20 });
    const query = sessionListQuery(request);
    expect(query).toContain("limit=20");
    expect(query).toContain("sort_by=updated_at");
    expect(query).toContain("order=desc");
    expect(query).toContain("kind=default");
    expect(query).toContain("include_archived=false");
    expect(query).toContain("after=conv+a");
    expect(query).not.toMatch(/search_query|project|pinned|agent_id/);
  });

  it.each([0, 26, -1, 1.5, "20", Number.NaN])("rejects invalid limit %p", (limit) => {
    expect(() => parseSessionPageRequest({ limit })).toThrow(ExtensionHostServiceError);
  });

  it.each(["", 123, "x".repeat(257)])("rejects invalid cursor %p", (after) => {
    expect(() => parseSessionPageRequest({ after })).toThrow("after cursor is invalid");
  });

  it("defaults to a bounded page", () => {
    expect(parseSessionPageRequest({})).toEqual({ after: null, limit: 25 });
    expect(parseSessionPageRequest({ limit: 1 })).toEqual({ after: null, limit: 1 });
    expect(parseSessionPageRequest({ limit: 25 })).toEqual({ after: null, limit: 25 });
  });
});

describe("projectSessionPage", () => {
  it("returns exactly the public projection and bounds strings", () => {
    const result = projectSessionPage(
      {
        data: [
          {
            ...wireRow,
            title: "t".repeat(400),
            workspace: "w".repeat(900),
          },
        ],
        has_more: false,
        last_id: "conv_1",
      },
      25,
    );

    expect(Object.keys(result.sessions[0]).sort()).toEqual([
      "createdAt",
      "gitBranch",
      "id",
      "projectId",
      "status",
      "title",
      "titleProvisional",
      "unread",
      "updatedAt",
      "workspace",
    ]);
    expect(result.sessions[0].title).toHaveLength(256);
    expect(result.sessions[0].projectId).toBe("project-secret");
    expect(result.sessions[0].workspace).toHaveLength(512);
    expect(result.nextCursor).toBeNull();
  });

  it("flags a finished session unread with the sidebar's read-state rule", () => {
    markConversationUnread("conv_1", wireRow.updated_at);
    const page = { data: [{ ...wireRow, status: "idle" }], has_more: false, last_id: null };
    expect(projectSessionPage(page, 25).sessions[0].unread).toBe(true);
    expect(projectSessionPage({ ...page, data: [wireRow] }, 25).sessions[0].unread).toBe(false);
  });

  it("falls back to the sidebar's provisional first-message title", () => {
    recordOptimisticTitle("conv_1", "Bob, please look at the flaky test");
    const untitled = { data: [{ ...wireRow, title: null }], has_more: false, last_id: null };
    expect(projectSessionPage(untitled, 25).sessions[0]).toMatchObject({
      title: "Bob, please look at the flaky test",
      titleProvisional: true,
    });
    const titled = projectSessionPage({ ...untitled, data: [wireRow] }, 25).sessions[0];
    expect(titled).toMatchObject({ title: "Session one", titleProvisional: false });
  });

  it("keeps a worst-case page within the RPC response budget", () => {
    const result = projectSessionPage(
      {
        data: Array.from({ length: 25 }, (_, index) => ({
          ...wireRow,
          id: `${index}`.padEnd(256, "i"),
          title: "t".repeat(400),
          workspace: "w".repeat(900),
        })),
        has_more: false,
        last_id: null,
      },
      25,
    );
    expect(
      isExtensionPayloadWithinBudget({
        source: "omnigent-extension",
        type: "response",
        extensionId: "acme.canvas",
        pageId: "acme.canvas.page",
        view: "canvas",
        nonce: "n".repeat(48),
        apiVersion: 1,
        requestId: "request",
        result,
      }),
    ).toBe(true);
  });

  it("uses last_id or the last row id only when another page exists", () => {
    expect(
      projectSessionPage({ data: [wireRow], has_more: true, last_id: "cursor" }, 25).nextCursor,
    ).toBe("cursor");
    expect(
      projectSessionPage({ data: [wireRow], has_more: true, last_id: null }, 25).nextCursor,
    ).toBe("conv_1");
  });

  it.each([
    { data: "wrong", has_more: false },
    { data: [{}], has_more: false },
    { data: [{ ...wireRow, status: "unknown" }], has_more: false },
    { data: [{ ...wireRow, updated_at: "wrong" }], has_more: false },
    { data: [wireRow], has_more: "yes" },
    { data: [], has_more: true, last_id: null },
    { data: Array.from({ length: 3 }, () => wireRow), has_more: false },
  ])("rejects malformed page %#", (payload) => {
    expect(() => projectSessionPage(payload, 2)).toThrow(ExtensionHostServiceError);
  });
});

describe("listSessionPage", () => {
  it("forwards cancellation and projects the server response", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ data: [wireRow], has_more: false, last_id: "conv_1" }), {
        status: 200,
      }),
    );
    const controller = new AbortController();

    const result = await listSessionPage({}, controller.signal);

    expect(result.sessions[0].id).toBe("conv_1");
    expect(authenticatedFetch).toHaveBeenCalledWith(
      expect.stringContaining("include_archived=false"),
      { signal: controller.signal },
    );
  });

  it("does not fetch with a pre-aborted signal", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(listSessionPage({}, controller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it.each([
    [403, "PermissionDenied"],
    [404, "HostError"],
    [500, "Unavailable"],
  ] as const)("maps HTTP %s to %s", async (status, code) => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status }));

    await expect(listSessionPage({}, new AbortController().signal)).rejects.toMatchObject({
      code,
    });
  });

  it("rejects non-JSON success responses", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("not-json", { status: 200 }));

    await expect(listSessionPage({}, new AbortController().signal)).rejects.toMatchObject({
      code: "HostError",
    });
  });
});
