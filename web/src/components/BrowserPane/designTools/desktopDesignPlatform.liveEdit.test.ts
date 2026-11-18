import { describe, expect, it, vi } from "vitest";
import type { DesignPage } from "./core/designPlatform";
import type { Answer } from "./desktopBridge";
import { desktopDesignPlatform } from "./desktopDesignPlatform";
import type { DesignOverlay } from "./desktopOverlay";

function fakeOverlay(ends: Answer[] = []) {
  let begun = 0;
  const request = vi.fn(async (op: string, _args?: Record<string, unknown>): Promise<Answer> =>
    op === "liveEditBegin" ? { ok: true, scope: `main_${++begun}` } : { ok: true, result: "ok" },
  );
  const endLiveEdit = vi.fn(
    async (_scope: string, _texts: unknown, _restore?: boolean): Promise<Answer> =>
      ends.shift() ?? { ok: true },
  );
  const overlay = { request, endLiveEdit } as unknown as DesignOverlay;
  return { overlay, request, endLiveEdit };
}

const pageOf = async (overlay: DesignOverlay) =>
  (await desktopDesignPlatform(overlay).page("tab_1")) as DesignPage;

describe("desktopDesignPlatform: Live Edit", () => {
  it("asks main by main's scope for the session's, begun once on first use", async () => {
    const { overlay, request } = fakeOverlay();
    const page = await pageOf(overlay);
    await page.liveEditPick("core_1", 3, 4);
    await page.liveEditPreview("core_1", 1, "New");
    await page.liveEditRestore("core_1");
    await page.liveEditPick("core_2", 5, 6);
    expect(request.mock.calls).toEqual([
      ["liveEditBegin"],
      ["liveEditPick", { scope: "main_1", x: 3, y: 4 }],
      ["liveEditPreview", { scope: "main_1", handle: 1, text: "New" }],
      ["liveEditRestore", { scope: "main_1" }],
      ["liveEditBegin"],
      ["liveEditPick", { scope: "main_2", x: 5, y: 6 }],
    ]);
  });

  it("ends a scope as asked, and only one that began in the page", async () => {
    const { overlay, endLiveEdit } = fakeOverlay();
    const page = await pageOf(overlay);
    expect(await page.liveEditSettle("never_used", [[1, "x"]])).toBe("");
    expect(endLiveEdit).not.toHaveBeenCalled();
    await page.liveEditPick("core_1", 1, 1);
    await page.liveEditDiscard("core_1");
    expect(endLiveEdit).toHaveBeenLastCalledWith("main_1", null, true);
    await page.liveEditPick("core_2", 1, 1);
    await page.liveEditForget("core_2");
    expect(endLiveEdit).toHaveBeenLastCalledWith("main_2", null, false);
  });

  it("keeps a scope whose end failed, so the session's second try still ends it", async () => {
    const { overlay, endLiveEdit } = fakeOverlay([
      { ok: false, error: "No answer" },
      { ok: true, result: "done" },
    ]);
    const page = await pageOf(overlay);
    await page.liveEditPick("core_1", 1, 1);
    await expect(page.liveEditSettle("core_1", [[1, "New"]])).rejects.toThrow("No answer");
    expect(await page.liveEditSettle("core_1", [[1, "New"]])).toBe("done");
    expect(endLiveEdit).toHaveBeenLastCalledWith("main_1", [[1, "New"]], false);
    // Once ended, nothing more to ask main.
    expect(await page.liveEditForget("core_1")).toBe("");
    expect(endLiveEdit).toHaveBeenCalledTimes(2);
  });

  it("begins again after a begin that failed", async () => {
    const { overlay, request } = fakeOverlay();
    request.mockResolvedValueOnce({ ok: true });
    const page = await pageOf(overlay);
    await expect(page.liveEditPick("core_1", 1, 1)).rejects.toThrow("Live Edit couldn't start.");
    await page.liveEditPick("core_1", 1, 1);
    expect(request).toHaveBeenLastCalledWith("liveEditPick", { scope: "main_1", x: 1, y: 1 });
  });
});
