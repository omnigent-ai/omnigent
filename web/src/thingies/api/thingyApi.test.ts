import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import { ApiError } from "@/lib/sessionsApi";

import {
  ThingyApiError,
  getThingy,
  grantThingyPermission,
  listSharedThingies,
  listThingyPermissions,
  revokeThingyPermission,
} from "./thingyApi";

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

const WIRE_THINGY = {
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

describe("thingy API", () => {
  it("reads a thingy from the thingy routes", async () => {
    fetchMock.mockResolvedValueOnce(json(WIRE_THINGY));

    expect(await getThingy("app/1")).toEqual(WIRE_THINGY);
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/thingies/app%2F1");
  });

  it("raises the server's code, request ID and details as an ApiError", async () => {
    fetchMock.mockResolvedValueOnce(
      json(
        {
          error: {
            code: "CONFLICT",
            message: "The thingy changed.",
            request_id: "req-1",
            details: { reason: "REVISION_CHANGED" },
          },
        },
        409,
      ),
    );

    const error = await getThingy("app-1").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ThingyApiError);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "CONFLICT",
      message: "The thingy changed.",
      requestId: "req-1",
      details: { reason: "REVISION_CHANGED" },
    });
  });

  it("reports a sign-in page instead of JSON as an expired sign-in", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } }),
    );

    await expect(getThingy("app-1")).rejects.toMatchObject({ status: 401 });
  });

  it("rejects a thingy the server returned without its permissions", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...WIRE_THINGY, permissions: undefined }));

    await expect(getThingy("app-1")).rejects.toMatchObject({ code: "INTERNAL" });
  });

  it("lists thingies shared with the viewer", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ items: [{ ...WIRE_THINGY, shared_via: "group", owner_email: "owner@example.com" }] }),
    );

    const shared = await listSharedThingies();

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/thingies/shared");
    expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined();
    expect(shared).toEqual([
      { ...WIRE_THINGY, shared_via: "group", owner_email: "owner@example.com" },
    ]);
  });

  it("follows every page of a thingy's grants", async () => {
    const grant = (userId: string) => ({
      user_id: userId,
      thingy_id: "app-1",
      level: 2,
      display_name: userId,
    });
    fetchMock
      .mockResolvedValueOnce(json({ permissions: [grant("a@example.com")], next_cursor: "c1" }))
      .mockResolvedValueOnce(json({ permissions: [grant("b@example.com")], next_cursor: null }));

    const grants = await listThingyPermissions("app-1");

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      "/v1/thingies/app-1/permissions",
      "/v1/thingies/app-1/permissions?after=c1",
    ]);
    expect(grants.map((item) => item.user_id)).toEqual(["a@example.com", "b@example.com"]);
  });

  it("grants and revokes access to a thingy", async () => {
    fetchMock
      .mockResolvedValueOnce(
        json({ user_id: "team", thingy_id: "app-1", level: 1, display_name: "Team" }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    expect(await grantThingyPermission("app-1", "team", 1)).toMatchObject({ level: 1 });
    await revokeThingyPermission("app-1", "a b");

    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ user_id: "team", level: 1 }),
    });
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/thingies/app-1/permissions/a%20b");
    expect(fetchMock.mock.calls[1][1]?.method).toBe("DELETE");
  });
});
