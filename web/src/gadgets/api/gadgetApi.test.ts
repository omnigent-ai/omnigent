import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

import {
  GadgetApiError,
  getGadget,
  getGadgetShelf,
  getSessionGadgets,
  listGadgetComments,
  listGadgets,
  listSharedGadgets,
} from "./gadgetApi";

const fetchMock = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>();
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: (input: RequestInfo | URL, init?: RequestInit) => fetchMock(input, init),
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const WIRE_GADGET = {
  id: "app-1",
  name: "Team board",
  owner_id: "owner@example.com",
  viewer_id: "viewer@example.com",
  viewer_principal_id: "42",
  published_revision: null,
  version: "v1",
  permissions: { read: true, write_data: true, publish: true, manage: false },
};

afterEach(() => {
  fetchMock.mockReset();
});

describe("gadget API", () => {
  it("reads a gadget from the gadget routes", async () => {
    fetchMock.mockResolvedValueOnce(json(WIRE_GADGET));

    expect(await getGadget("app/1")).toEqual(WIRE_GADGET);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/gadgets/app%2F1");
  });

  it("raises the server's code, request ID and details as an ApiError", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "CONFLICT",
            message: "The gadget changed.",
            request_id: "req-1",
            details: { reason: "REVISION_CHANGED" },
          },
        },
        409,
      ),
    );

    const error = await getGadget("app-1").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(GadgetApiError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "CONFLICT",
      message: "The gadget changed.",
      requestId: "req-1",
      details: { reason: "REVISION_CHANGED" },
    });
  });

  it("reports a sign-in page instead of JSON as an expired sign-in", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } }),
    );

    await expect(getGadget("app-1")).rejects.toMatchObject({ status: 401 });
  });

  it("rejects a gadget the server returned without its permissions", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...WIRE_GADGET, permissions: undefined }));

    await expect(getGadget("app-1")).rejects.toMatchObject({ code: "INTERNAL" });
  });

  it("lists gadgets shared with the viewer", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ items: [{ ...WIRE_GADGET, shared_via: "group", owner_email: "owner@example.com" }] }),
    );

    const shared = await listSharedGadgets();

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/gadgets/shared?limit=100");
    expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined();
    expect(shared).toEqual([
      { ...WIRE_GADGET, shared_via: "group", owner_email: "owner@example.com" },
    ]);
  });

  it("follows every page of the viewer's gadgets and lists them oldest first", async () => {
    fetchMock
      .mockResolvedValueOnce(
        json({ items: [{ ...WIRE_GADGET, id: "b", created_at: 20 }], next_cursor: "b" }),
      )
      .mockResolvedValueOnce(json({ items: [], next_cursor: "c" }))
      .mockResolvedValueOnce(
        json({ items: [{ ...WIRE_GADGET, id: "d", created_at: 10 }], next_cursor: null }),
      );

    const gadgets = await listGadgets();

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/v1/gadgets?limit=100",
      "/v1/gadgets?limit=100&cursor=b",
      "/v1/gadgets?limit=100&cursor=c",
    ]);
    expect(gadgets.map((gadget) => gadget.id)).toEqual(["d", "b"]);
  });

  it("follows every page of the pins and of the gadgets shown beside a session", async () => {
    fetchMock
      .mockResolvedValueOnce(
        json({ pinned: [{ id: "a", name: "A" }], has_owned: true, next_cursor: "p1" }),
      )
      .mockResolvedValueOnce(
        json({ pinned: [{ id: "b", name: "B" }], has_owned: true, next_cursor: null }),
      )
      .mockResolvedValueOnce(json({ items: [{ ...WIRE_GADGET, id: "a" }], next_cursor: "p1" }))
      .mockResolvedValueOnce(json({ items: [{ ...WIRE_GADGET, id: "b" }], next_cursor: null }));

    const shelf = await getGadgetShelf();
    const beside = await getSessionGadgets("conv_1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/v1/gadgets/shelf?limit=50",
      "/v1/gadgets/shelf?limit=50&cursor=p1",
      "/v1/gadgets/sessions/conv_1?limit=20",
      "/v1/gadgets/sessions/conv_1?limit=20&cursor=p1",
    ]);
    expect(shelf).toEqual({
      pinned: [
        { id: "a", name: "A" },
        { id: "b", name: "B" },
      ],
      hasOwned: true,
    });
    expect(beside.map((gadget) => gadget.id)).toEqual(["a", "b"]);
  });

  it("reads comments past a short page and sorts them by time", async () => {
    const comment = (id: string, createdAt: number) => ({
      id,
      body: id,
      status: "draft",
      anchor: { text: "x" },
      created_by: "a@example.com",
      created_at: createdAt,
      updated_at: createdAt,
    });
    fetchMock
      .mockResolvedValueOnce(json({ items: [], next_cursor: "k", can_comment: true }))
      .mockResolvedValueOnce(
        json({ items: [comment("z", 1), comment("a", 2)], next_cursor: null, can_comment: true }),
      );

    const listed = await listGadgetComments("app-1");

    expect(fetchMock.mock.calls[1][0]).toBe("/v1/gadgets/app-1/comments?limit=100&cursor=k");
    expect(listed.items.map((item) => item.id)).toEqual(["z", "a"]);
    expect(listed.canComment).toBe(true);
  });

  it("follows more than a hundred short pages, and refuses a cursor that loops", async () => {
    const comment = (id: string, createdAt: number) => ({
      id,
      body: id,
      status: "draft",
      anchor: { text: "x" },
      created_by: "a@example.com",
      created_at: createdAt,
      updated_at: createdAt,
    });
    // Large comments: a few fit a page, so 601 of them take 101 pages.
    for (let page = 0; page < 101; page++) {
      fetchMock.mockResolvedValueOnce(
        json({
          items: [comment(`c${page}`, page)],
          next_cursor: page < 100 ? `k${page}` : null,
          can_comment: true,
        }),
      );
    }
    const listed = await listGadgetComments("app-1");
    expect(listed.items).toHaveLength(101);
    expect(listed.items.at(-1)?.id).toBe("c100");
    expect(listed.canComment).toBe(true);

    fetchMock
      .mockResolvedValueOnce(json({ items: [], next_cursor: "loop", can_comment: true }))
      .mockResolvedValueOnce(json({ items: [], next_cursor: "loop", can_comment: true }));
    await expect(listGadgetComments("app-1")).rejects.toThrow();
  });
});
