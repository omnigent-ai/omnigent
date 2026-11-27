import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { getSessionParent } from "@/lib/sessionHost";
import { fetchSessionDescendants, SUB_AGENT_LIST_CAP } from "./useSessionDescendants";

/** A read that answers only by failing once its signal aborts, as a cancelled fetch does. */
const pendingUntilAborted = (signal: AbortSignal | undefined) =>
  new Promise<Response>((_resolve, reject) => {
    signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  });

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
      conv_root: ["conv_walk_a", "conv_walk_b"],
      conv_walk_a: ["conv_walk_a1"],
      conv_walk_b: ["conv_walk_b1", "conv_walk_b2"],
    };
    // Each child's parent, as registered when the level below it is read.
    const parentsAtRead: Record<string, string | null> = {};
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const parent = Object.keys(tree).find((id) => String(input).includes(`/${id}/`));
      if (parent && parent !== "conv_root") parentsAtRead[parent] = getSessionParent(parent);
      return page((tree[parent ?? ""] ?? []).map((id) => row(id)));
    });

    const result = await fetchSessionDescendants("conv_root");

    expect(parentsAtRead).toEqual({ conv_walk_a: "conv_root", conv_walk_b: "conv_root" });
    expect(result.rows.map((entry) => [entry.info.id, entry.parentId, entry.depth])).toEqual([
      ["conv_walk_a", "conv_root", 1],
      ["conv_walk_b", "conv_root", 1],
      ["conv_walk_a1", "conv_walk_a", 2],
      ["conv_walk_b1", "conv_walk_b", 2],
      ["conv_walk_b2", "conv_walk_b", 2],
    ]);
    expect(getSessionParent("conv_walk_b2")).toBe("conv_walk_b");
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

  it.each([
    ["null depths", (id: string) => row(id, { depth: null })],
    ["a deeper row without its parent", (id: string) => row(id, { depth: 2 })],
  ])(
    "walks the tree when the listing has %s, so no grandchild is left out",
    async (_case, listed) => {
      vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
        const url = new URL(String(input), "http://test.local");
        // The listing answers like the direct children; the walk then reads each child's own.
        if (url.pathname.includes("conv_position_root"))
          return page([listed("conv_position_child")]);
        if (url.pathname.includes("conv_position_child"))
          return page([row("conv_position_grandchild")]);
        return page([]);
      });

      const result = await fetchSessionDescendants("conv_position_root");

      expect(result.rows.map((entry) => [entry.info.id, entry.parentId, entry.depth])).toEqual([
        ["conv_position_child", "conv_position_root", 1],
        ["conv_position_grandchild", "conv_position_child", 2],
      ]);
      expect(getSessionParent("conv_position_grandchild")).toBe("conv_position_child");
      expect(result.truncated).toBe(false);
    },
  );

  it("cancels the siblings it read ahead once the first parent fills the cap, and reads no others", async () => {
    const siblings = ["conv_cap_b", "conv_cap_c", "conv_cap_d", "conv_cap_e", "conv_cap_f"];
    const signals = new Map<string, AbortSignal | undefined>();
    let next = 0;
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://test.local");
      const parent = url.pathname.split("/")[3]!;
      // No depths, so the hook walks: the root lists its children, then each child's.
      if (parent === "conv_cap_root") return page(["conv_cap_a", ...siblings].map((id) => row(id)));
      if (parent === "conv_cap_a") {
        const rows = Array.from({ length: 200 }, () => row(`conv_cap_child_${(next += 1)}`));
        return page(rows, rows.at(-1)!.id);
      }
      signals.set(parent, init?.signal ?? undefined);
      return pendingUntilAborted(init?.signal ?? undefined);
    });

    const result = await fetchSessionDescendants("conv_cap_root");

    expect(result.truncated).toBe(true);
    expect(result.rows).toHaveLength(SUB_AGENT_LIST_CAP);
    expect(ids(result).slice(0, 7)).toEqual(["conv_cap_a", ...siblings, "conv_cap_child_1"]);
    // Only the next three parents were read ahead, and each read was cancelled.
    expect([...signals.keys()]).toEqual(["conv_cap_b", "conv_cap_c", "conv_cap_d"]);
    expect([...signals.values()].every((signal) => signal?.aborted)).toBe(true);
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
