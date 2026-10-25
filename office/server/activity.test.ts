import { describe, expect, it } from "vitest";
import { type ItemsPage, type WireItem, isHumanMessage, latestTurnError, mergeScan, scanSession } from "./activity.ts";

const human = (id: string, at: number): WireItem => ({ id, type: "message", role: "user", created_at: at });
const agent = (id: string, at: number): WireItem => ({ id, type: "message", role: "assistant", created_at: at });

function pagedFetch(pages: WireItem[][]) {
  const calls: (string | null)[] = [];
  return {
    calls,
    fetch: async (_sessionId: string, after: string | null): Promise<ItemsPage> => {
      calls.push(after);
      const index = calls.length - 1;
      const data = pages[index] ?? [];
      return { data, has_more: index < pages.length - 1, last_id: data.at(-1)?.id ?? null };
    },
  };
}

describe("isHumanMessage", () => {
  it("counts human input but not injected context or agent output", () => {
    expect(isHumanMessage(human("h", 1))).toBe(true);
    expect(isHumanMessage(agent("a", 1))).toBe(false);
    expect(isHumanMessage({ ...human("meta", 1), is_meta: true })).toBe(false);
    expect(isHumanMessage({ ...human("relay", 1), user_authored: false })).toBe(false);
    expect(isHumanMessage({ id: "tool", type: "function_call", created_at: 1 })).toBe(false);
  });
});

describe("scanSession", () => {
  it("pages newest-first until it finds the latest human message", async () => {
    const source = pagedFetch([[agent("a3", 30), agent("a2", 20)], [human("h1", 10), agent("a0", 5)]]);
    const scan = await scanSession("conv", 99, source.fetch);
    expect(scan).toEqual({ scannedUpdatedAt: 99, latestHumanItemId: "h1", latestHumanAt: 10, complete: true, lastTurnError: null });
    expect(source.calls).toEqual([null, "a2"]);
  });

  it("reports complete with no interaction when history ends without human input", async () => {
    const scan = await scanSession("conv", 1, pagedFetch([[agent("a", 1)]]).fetch);
    expect(scan.complete).toBe(true);
    expect(scan.latestHumanAt).toBeNull();
  });

  it("reports incomplete when the page budget runs out", async () => {
    const pages = Array.from({ length: 4 }, (_, index) => [agent(`a${index}`, index)]);
    const scan = await scanSession("conv", 1, pagedFetch(pages).fetch, 2);
    expect(scan.complete).toBe(false);
  });
});

describe("latestTurnError", () => {
  const error = (id: string, at: number, extra: Partial<WireItem> = {}): WireItem => ({
    id,
    type: "error",
    created_at: at,
    source: "llm",
    code: "rate_limit_exceeded",
    message: "429 Too Many Requests",
    ...extra,
  });

  it("reports the error that ended the latest turn, skipping metadata items", () => {
    const found = latestTurnError([{ id: "r", type: "reasoning" }, error("e", 30, { remediation: "Wait a minute" }), agent("a", 20)]);
    expect(found).toEqual({ code: "rate_limit_exceeded", message: "429 Too Many Requests", source: "llm", title: undefined, cause: undefined, remediation: "Wait a minute", at: 30 });
  });

  it("clears once later output or a new message follows, and ignores informational banners", () => {
    expect(latestTurnError([agent("a", 40), error("e", 30)])).toBeNull();
    expect(latestTurnError([human("h", 40), error("e", 30)])).toBeNull();
    expect(latestTurnError([error("notice", 40, { level: "info" }), agent("a", 30)])).toBeNull();
  });

  it("is recorded by the scan from the newest page", async () => {
    const scan = await scanSession("conv", 5, pagedFetch([[error("e", 30), human("h", 10)]]).fetch);
    expect(scan.lastTurnError?.code).toBe("rate_limit_exceeded");
  });
});

describe("mergeScan", () => {
  it("never loses a known interaction to a later incomplete rescan", () => {
    const previous = { scannedUpdatedAt: 1, latestHumanItemId: "h", latestHumanAt: 50, complete: true };
    const rescan = { scannedUpdatedAt: 2, latestHumanItemId: null, latestHumanAt: null, complete: false };
    expect(mergeScan(previous, rescan)).toEqual({ ...previous, scannedUpdatedAt: 2 });
  });

  it("is idempotent for a replayed scan of the same message", () => {
    const scan = { scannedUpdatedAt: 1, latestHumanItemId: "h", latestHumanAt: 50, complete: true };
    expect(mergeScan(scan, { ...scan })).toEqual(scan);
  });
});

describe("scanSession with office relays", () => {
  it("skips messages the office relayed between agents", async () => {
    const { fetch } = pagedFetch([[human("relay_1", 30), agent("a", 25), human("h", 20)]]);
    const scan = await scanSession("conv", 30, fetch, 5, (itemId) => itemId === "relay_1");
    expect(scan.latestHumanItemId).toBe("h");
    expect(scan.latestHumanAt).toBe(20);
  });
});
