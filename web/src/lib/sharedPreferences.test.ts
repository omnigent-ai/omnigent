import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { readSharedPreferences } from "./sharedPreferences";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
const fetcher = vi.mocked(authenticatedFetch);
const identity = { authority_id: "test", workspace_id: "workspace", user_id: "alice" };
const first = {
  protocol_version: 2,
  identity,
  revision: 2,
  entries: [{ key: { kind: "model", name: "model" }, value: "selected", version: 1 }],
  conflicts: [],
  next_cursor: "page2",
};
beforeEach(() => fetcher.mockReset());

describe("complete preference reads", () => {
  it("assembles pages before returning a snapshot", async () => {
    fetcher.mockResolvedValueOnce(Response.json(first)).mockResolvedValueOnce(
      Response.json({
        ...first,
        entries: [{ key: { kind: "harness", name: "default" }, version: 1, value: "pi-native" }],
        next_cursor: null,
      }),
    );
    const snapshot = await readSharedPreferences();
    expect(snapshot.entries.map((entry) => entry.value)).toEqual(["selected", "pi-native"]);
    expect(fetcher.mock.calls[0][0]).toBe("/v1/preferences/shared?protocol_version=2");
    expect(fetcher.mock.calls[1][0]).toContain("cursor=page2");
  });
  it.each([
    { identity: { ...identity, user_id: "bob" } },
    { revision: 3 },
    { protocol_version: 1 },
    { entries: first.entries },
    { next_cursor: "page2" },
  ])("rejects inconsistent continuation %j", async (fault) => {
    fetcher
      .mockResolvedValueOnce(Response.json(first))
      .mockResolvedValueOnce(Response.json({ ...first, entries: [], next_cursor: null, ...fault }));
    await expect(readSharedPreferences()).rejects.toThrow();
  });
  it("does not return a partial snapshot after a later page fails", async () => {
    fetcher.mockResolvedValueOnce(Response.json(first)).mockRejectedValueOnce(new Error("offline"));
    await expect(readSharedPreferences()).rejects.toThrow("offline");
  });
});
