import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchSessionDescendants } from "./useSessionDescendants";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const STALE = '{"error":{"code":"stale_cursor"}}';
const page = (data: object[], lastId?: string) =>
  new Response(JSON.stringify({ data, ...(lastId ? { last_id: lastId, has_more: true } : {}) }));
const row = (id: string, extra: object = {}) => ({ id, busy: false, ...extra });
const ids = (result: Awaited<ReturnType<typeof fetchSessionDescendants>>) =>
  result.rows.map((entry) => entry.info.id);

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

describe("fetchSessionDescendants", () => {
  it("lists the tree again once when a later page's cursor goes stale", async () => {
    let stale = true;
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const after = new URL(String(input), "http://test.local").searchParams.get("after");
      if (after === null) return page([row("conv_a", { depth: 1 })], "conv_a");
      if (stale) {
        stale = false;
        return new Response(STALE, { status: 400 });
      }
      return page([row("conv_b", { depth: 1 })]);
    });

    const result = await fetchSessionDescendants("conv_root");

    expect(ids(result)).toEqual(["conv_a", "conv_b"]);
    // The first page, the stale one, then both pages again.
    expect(authenticatedFetch).toHaveBeenCalledTimes(4);
  });

  it("walks the tree again once when a level's cursor goes stale", async () => {
    let stale = true;
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = new URL(String(input), "http://test.local");
      const after = url.searchParams.get("after");
      // No depths, as an older server answers, so the walk reads each parent's children.
      if (url.pathname.includes("conv_root")) {
        if (after === null) return page([row("conv_a")], "conv_a");
        if (stale) {
          stale = false;
          return new Response(STALE, { status: 400 });
        }
        return page([row("conv_b")]);
      }
      return page([]);
    });

    const result = await fetchSessionDescendants("conv_root");

    expect(ids(result)).toEqual(["conv_a", "conv_b"]);
    expect(result.truncated).toBe(false);
  });

  it("fails on a second stale cursor, and on any other refusal", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const after = new URL(String(input), "http://test.local").searchParams.get("after");
      return after === null
        ? page([row("conv_a", { depth: 1 })], "conv_a")
        : new Response(STALE, { status: 400 });
    });
    await expect(fetchSessionDescendants("conv_root")).rejects.toMatchObject({ status: 400 });
    expect(authenticatedFetch).toHaveBeenCalledTimes(4);

    vi.mocked(authenticatedFetch).mockReset();
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("{}", { status: 500 }));
    await expect(fetchSessionDescendants("conv_root")).rejects.toMatchObject({ status: 500 });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });
});
