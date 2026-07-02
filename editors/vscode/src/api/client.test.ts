/**
 * Tests for the minimal /v1 client: pure `accumulateSessions` plus `listSessions`
 * driven through an injected `fetchImpl` stub (no real network).
 */
import { describe, it, expect } from "vitest";
import {
  accumulateSessions,
  listSessions,
  type ClientOptions,
  type Session,
  type SessionsPage,
} from "./client";

function session(id: string, over: Partial<Session> = {}): Session {
  return { id, ...over };
}

function page(data: Session[], over: Partial<SessionsPage> = {}): SessionsPage {
  return { object: "list", data, has_more: false, ...over };
}

/** A fetchImpl stub that returns the given pages in sequence as JSON 200s. */
function pagedFetch(pages: SessionsPage[]): ClientOptions {
  let call = 0;
  const fetchImpl = (async () => {
    const body = pages[Math.min(call, pages.length - 1)];
    call += 1;
    return {
      ok: true,
      status: 200,
      json: async () => body,
    } as Response;
  }) as unknown as typeof fetch;
  return { baseUrl: "http://127.0.0.1:6767", fetchImpl };
}

describe("accumulateSessions", () => {
  it("concatenates a single page in order, not truncated", () => {
    const r = accumulateSessions([page([session("a"), session("b")])], 200);
    expect(r.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(r.truncated).toBe(false);
  });

  it("concatenates multiple pages in order", () => {
    const r = accumulateSessions(
      [
        page([session("a")], { has_more: true, last_id: "a" }),
        page([session("b")], { has_more: false }),
      ],
      200,
    );
    expect(r.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(r.truncated).toBe(false);
  });

  it("stops at the cap and reports truncated when has_more persists", () => {
    const r = accumulateSessions(
      [page([session("a"), session("b"), session("c")], { has_more: true, last_id: "c" })],
      2,
    );
    expect(r.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(r.truncated).toBe(true);
  });

  it("is NOT truncated when the cap is reached but has_more is false", () => {
    const r = accumulateSessions([page([session("a"), session("b")], { has_more: false })], 2);
    expect(r.truncated).toBe(false);
  });
});

describe("listSessions", () => {
  it("returns a single page when has_more is false", async () => {
    const opts = pagedFetch([page([session("a"), session("b")])]);
    const r = await listSessions(opts);
    expect(r.ok).toBe(true);
    expect(r.data?.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(r.data?.truncated).toBe(false);
  });

  it("follows the last_id cursor across pages while has_more is true", async () => {
    const opts = pagedFetch([
      page([session("a")], { has_more: true, last_id: "a" }),
      page([session("b")], { has_more: true, last_id: "b" }),
      page([session("c")], { has_more: false }),
    ]);
    const r = await listSessions(opts);
    expect(r.data?.sessions.map((s) => s.id)).toEqual(["a", "b", "c"]);
    expect(r.data?.truncated).toBe(false);
  });

  it("stops following the cursor once the cap is reached and reports truncated", async () => {
    const opts = pagedFetch([
      page([session("a"), session("b")], { has_more: true, last_id: "b" }),
    ]);
    const r = await listSessions(opts, 2);
    expect(r.data?.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(r.data?.truncated).toBe(true);
  });

  it("does not loop forever on an empty page that still reports has_more", async () => {
    // A misbehaving server: empty data but has_more:true and a cursor. Without the
    // empty-page guard this would spin (pagedFetch repeats the last page forever).
    const opts = pagedFetch([page([], { has_more: true, last_id: "x" })]);
    const r = await listSessions(opts);
    expect(r.ok).toBe(true);
    expect(r.data?.sessions).toEqual([]);
  });

  it("does not loop forever when the cursor fails to advance", async () => {
    // has_more:true but last_id never changes — the non-advancing-cursor guard
    // breaks after the repeat instead of paging up to the cap.
    const opts = pagedFetch([page([session("a")], { has_more: true, last_id: "a" })]);
    const r = await listSessions(opts, 200);
    expect(r.ok).toBe(true);
    expect(r.data!.sessions.length).toBeLessThanOrEqual(2);
  });

  it("propagates a network failure as status 0 / not ok", async () => {
    const fetchImpl = (async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof fetch;
    const r = await listSessions({ baseUrl: "http://127.0.0.1:6767", fetchImpl });
    expect(r.ok).toBe(false);
    expect(r.status).toBe(0);
  });

  it("propagates a non-2xx status without throwing", async () => {
    const fetchImpl = (async () =>
      ({ ok: false, status: 500, json: async () => ({}) }) as Response) as unknown as typeof fetch;
    const r = await listSessions({ baseUrl: "http://127.0.0.1:6767", fetchImpl });
    expect(r.ok).toBe(false);
    expect(r.status).toBe(500);
  });

  it("parses optional fields and the archived boolean", async () => {
    const opts = pagedFetch([
      page([
        session("conv_1", {
          title: "Hello",
          agent_name: "claude",
          updated_at: 1000,
          archived: true,
        }),
        session("conv_2"),
      ]),
    ]);
    const r = await listSessions(opts);
    const [a, b] = r.data!.sessions;
    expect(a.title).toBe("Hello");
    expect(a.archived).toBe(true);
    expect(b.title).toBeUndefined();
    expect(b.archived).toBeUndefined();
  });
});
