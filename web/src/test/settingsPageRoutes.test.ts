import { describe, expect, it } from "vitest";

import { settingsRouteTable } from "./settingsPageRoutes";

describe("settingsRouteTable", () => {
  it("answers a listed request by method, path and exact query, and records the rest", async () => {
    const routes = settingsRouteTable([
      { url: "/v1/me", body: { user_id: "alice" } },
      { method: "POST", url: "/v1/agents?limit=50", body: { ok: true }, status: 201 },
    ]);
    const me = await routes.fetch("/v1/me");
    expect(me.status).toBe(200);
    expect(await me.json()).toEqual({ user_id: "alice" });
    expect((await routes.fetch("/v1/agents?limit=50", { method: "POST" })).status).toBe(201);
    expect((await routes.fetch(new URL("/v1/me", window.location.origin))).status).toBe(200);
    for (const unlisted of [
      "/v1/agents?limit=50",
      "/v1/agents?scope=user&limit=50",
      "/v1/me?x=1",
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- in order, as the record is checked in order
      expect((await routes.fetch(unlisted)).status).toBe(404);
    }
    expect(routes.unmatched).toEqual([
      "GET /v1/agents?limit=50",
      "GET /v1/agents?scope=user&limit=50",
      "GET /v1/me?x=1",
    ]);
  });

  it("keeps a Request's own method, so a DELETE never matches the GET route", async () => {
    const routes = settingsRouteTable([{ url: "/v1/me", body: { user_id: "alice" } }]);
    const response = await routes.fetch(
      new Request(new URL("/v1/me", window.location.origin), { method: "DELETE" }),
    );
    expect(response.status).toBe(404);
    expect(routes.unmatched).toEqual(["DELETE /v1/me"]);
    // An init's method still applies to a plain path.
    await routes.fetch("/v1/me", { method: "PATCH" });
    expect(routes.unmatched).toEqual(["DELETE /v1/me", "PATCH /v1/me"]);
  });

  it("records each request a route answered, in order, and not one it couldn't", async () => {
    const routes = settingsRouteTable([
      { url: "/v1/me", body: {} },
      { method: "DELETE", url: "/v1/logins/1", body: {} },
    ]);
    await routes.fetch("/v1/logins/1", { method: "DELETE" });
    await routes.fetch("/v1/unlisted");
    await routes.fetch("/v1/me");
    expect(routes.answered()).toEqual(["DELETE /v1/logins/1", "GET /v1/me"]);
  });

  it("answers a route differently once a scenario changes it", async () => {
    const routes = settingsRouteTable([{ url: "/v1/sharing", body: { mode: "on" } }]);
    routes.set({ url: "/v1/sharing", body: { detail: "Unavailable" }, status: 503 });
    expect((await routes.fetch("/v1/sharing")).status).toBe(503);
  });

  it("lists each request still waiting on its hold, until it's answered or its hold fails", async () => {
    let release!: () => void;
    let fail!: () => void;
    const routes = settingsRouteTable([
      { url: "/v1/me", body: {} },
      {
        method: "POST",
        url: "/v1/save",
        body: {},
        hold: new Promise<void>((done) => {
          release = done;
        }),
      },
      {
        method: "POST",
        url: "/v1/broken",
        body: {},
        hold: new Promise<void>((_, reject) => {
          fail = reject;
        }),
      },
    ]);
    await routes.fetch("/v1/me");
    expect(routes.inFlight()).toEqual([]);
    const first = routes.fetch("/v1/save", { method: "POST" });
    const second = routes.fetch("/v1/save", { method: "POST" });
    const broken = routes.fetch("/v1/broken", { method: "POST" });
    await Promise.resolve();
    expect(routes.inFlight()).toEqual(["POST /v1/save", "POST /v1/save", "POST /v1/broken"]);
    release();
    await Promise.all([first, second]);
    expect(routes.inFlight()).toEqual(["POST /v1/broken"]);
    fail();
    await expect(broken).rejects.toBeUndefined();
    expect(routes.inFlight()).toEqual([]);
  });
});
