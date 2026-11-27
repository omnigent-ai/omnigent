import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { getSessionParent } from "@/lib/sessionHost";
import { fetchSessionDescendants, SUB_AGENT_LIST_CAP } from "./useSessionDescendants";

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

  it("walks each parent's own children, and registers each row under its real parent", async () => {
    const tree: Record<string, string[]> = {
      conv_root: ["conv_a", "conv_b"],
      conv_a: ["conv_a1"],
      conv_b: ["conv_b1", "conv_b2"],
    };
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const parent = Object.keys(tree).find((id) => String(input).includes(`/${id}/`));
      return page((tree[parent ?? ""] ?? []).map((id) => row(id)));
    });

    const result = await fetchSessionDescendants("conv_root");

    expect(result.rows.map((entry) => [entry.info.id, entry.parentId, entry.depth])).toEqual([
      ["conv_a", "conv_root", 1],
      ["conv_b", "conv_root", 1],
      ["conv_a1", "conv_a", 2],
      ["conv_b1", "conv_b", 2],
      ["conv_b2", "conv_b", 2],
    ]);
    expect(getSessionParent("conv_b2")).toBe("conv_b");
    expect(result.truncated).toBe(false);
  });

  it("registers a grandchild from a depth-bearing answer under its real parent", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      page([
        row("conv_child", { depth: 1, parent_session_id: "conv_root" }),
        row("conv_grandchild", { depth: 2, parent_session_id: "conv_child" }),
      ]),
    );

    const result = await fetchSessionDescendants("conv_root");

    expect(result.rows.map((entry) => [entry.info.id, entry.parentId, entry.depth])).toEqual([
      ["conv_child", "conv_root", 1],
      ["conv_grandchild", "conv_child", 2],
    ]);
    expect(getSessionParent("conv_grandchild")).toBe("conv_child");
    // One read answers the whole tree.
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("stops the walk at the row cap and says the list is cut short", async () => {
    let next = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      if (!String(input).includes("/conv_root/")) return page([]);
      const rows = Array.from({ length: 100 }, () => row(`conv_${(next += 1)}`));
      return page(rows, rows.at(-1)!.id);
    });

    const result = await fetchSessionDescendants("conv_root");

    expect(result.rows).toHaveLength(SUB_AGENT_LIST_CAP);
    expect(result.truncated).toBe(true);
  });
});
