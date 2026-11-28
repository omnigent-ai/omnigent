// A tool session's end work (what Live Edit leaves the page with): it runs after the
// session's own page work, is asked once more if the page doesn't take it, and holds back
// the tab's next session until it's done.
import { afterEach, describe, expect, it, vi } from "vitest";

import { setTabDesignPlatform, type DesignPage, type PageOp } from "./designPlatform";
import { closeTool, openTool, resetDesignTools } from "./designToolsStore";
import { onEndFailed, toolSession } from "./toolSession";

const TAB = "browser-tab:conv_1:tab_2";
const run = vi.fn<(name: string) => Promise<string>>();
const designPage = { hitTest: () => run("hit") } as unknown as DesignPage;
setTabDesignPlatform(TAB, {
  page: async () => designPage,
  capture: {
    screenshot: async () => new File([], "x.png"),
    element: async () => new File([], "x.png"),
  },
});
const op =
  (name: string): PageOp =>
  () =>
    run(name);

/** A page whose replies wait until `answer` is called, in order. */
function slowPage() {
  const replies: ((value: string) => void)[] = [];
  const asked: string[] = [];
  run.mockImplementation(
    (name) =>
      new Promise<string>((resolve) => {
        asked.push(name);
        replies.push(resolve);
      }),
  );
  return { asked, answer: () => replies.shift()?.("ok") };
}

const flush = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  resetDesignTools();
  run.mockReset();
});

describe("tool session: its end", () => {
  it("runs its end work after the work queued before, even though it ended", async () => {
    const slow = slowPage();
    openTool(TAB, "edit");
    const session = toolSession(TAB)!;
    session.atEnd(() => op("forget"));
    void session.run(op("preview")).catch(() => undefined);
    await flush();
    closeTool(TAB);
    expect(slow.asked).toEqual(["preview"]);
    slow.answer();
    await flush();
    expect(slow.asked).toEqual(["preview", "forget"]);
  });

  it("runs no end work for a session that never used the page, or that skips it", async () => {
    run.mockResolvedValue("");
    openTool(TAB, "edit");
    toolSession(TAB)!.atEnd(() => op("unused"));
    closeTool(TAB);
    openTool(TAB, "edit");
    const skipping = toolSession(TAB)!;
    skipping.atEnd(() => null);
    await skipping.run(op("pick"));
    closeTool(TAB);
    await flush();
    expect(run.mock.calls.map(([name]) => name)).toEqual(["pick"]);
  });

  it("asks once more for end work the page didn't take, and says when it still doesn't", async () => {
    const failed = vi.fn();
    const stop = onEndFailed(failed);
    try {
      const endWith = async (settle: () => Promise<string>) => {
        run.mockReset();
        run.mockImplementation((name) => (name === "settle" ? settle() : Promise.resolve("")));
        openTool(TAB, "edit");
        const session = toolSession(TAB)!;
        session.atEnd(() => op("settle"));
        await session.run(op("preview"));
        closeTool(TAB);
        await flush();
        return run.mock.calls.map(([name]) => name);
      };
      let refusals = 1;
      const takenSecond = await endWith(async () => {
        if (refusals-- > 0) throw new Error("no answer");
        return "";
      });
      expect(takenSecond).toEqual(["preview", "settle", "settle"]);
      expect(failed).not.toHaveBeenCalled();
      const neverTaken = await endWith(async () => {
        throw new Error("no answer");
      });
      expect(neverTaken).toEqual(["preview", "settle", "settle"]);
      expect(failed).toHaveBeenCalledWith(TAB);
    } finally {
      stop();
    }
  });

  it("starts a tab's next session's page work after the last one's end", async () => {
    const slow = slowPage();
    openTool(TAB, "edit");
    const old = toolSession(TAB)!;
    old.atEnd(() => op("settle old"));
    void old.run(op("preview old")).catch(() => undefined);
    await flush();
    closeTool(TAB);
    openTool(TAB, "edit");
    void toolSession(TAB)!.run(op("pick new"));
    await flush();
    expect(slow.asked).toEqual(["preview old"]);
    slow.answer();
    await flush();
    expect(slow.asked).toEqual(["preview old", "settle old"]);
    slow.answer();
    await flush();
    expect(slow.asked).toEqual(["preview old", "settle old", "pick new"]);
  });

  it("gives each session a scope of its own", () => {
    openTool(TAB, "edit");
    const first = toolSession(TAB)!.scope;
    openTool(TAB, "edit");
    expect(toolSession(TAB)!.scope).not.toBe(first);
  });
});
