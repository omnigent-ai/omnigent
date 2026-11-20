import { afterEach, describe, expect, it, vi } from "vitest";

import { setTabDesignPlatform, type DesignPage, type PageOp } from "./designPlatform";
import { closeTool, openTool, resetDesignTools, tabTools } from "./designToolsStore";
import { isSessionEnded, toolSession } from "./toolSession";

const TAB = "browser-tab:conv_1:tab_1";

// A platform whose page does each operation through `page.run` (with a name for it), or
// that says why there's none.
const page = { run: vi.fn<(name: string) => Promise<string>>() };
const designPage: DesignPage = {
  hitTest: (x) => page.run(`hit ${x}`),
  scroll: () => page.run("scroll"),
};
/** An operation the page answers as `name`. */
const op =
  (name: string): PageOp =>
  () =>
    page.run(name);
let unavailable: string | null = null;
setTabDesignPlatform(TAB, {
  page: async () => unavailable ?? designPage,
  capture: {
    screenshot: async () => new File([], "x.png"),
    element: async () => new File([], "x.png"),
  },
});

/** A page whose replies wait until `answer` is called, in order. */
function slowPage() {
  const replies: ((value: string) => void)[] = [];
  const asked: string[] = [];
  page.run.mockImplementation(
    (name: string) =>
      new Promise<string>((resolve) => {
        asked.push(name);
        replies.push(resolve);
      }),
  );
  return {
    asked,
    answer: async (value = "ok") => {
      replies.shift()?.(value);
      await Promise.resolve();
      await Promise.resolve();
    },
  };
}

const flush = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  resetDesignTools();
  vi.useRealTimers();
  page.run.mockReset();
  unavailable = null;
});

describe("tool session", () => {
  it("starts with an open tool and ends when it closes or is replaced", () => {
    expect(toolSession(TAB)).toBeNull();
    openTool(TAB, "annotate");
    const first = toolSession(TAB)!;
    expect(first.id).toBe(tabTools(TAB).session);
    openTool(TAB, "draw");
    expect(first.isEnded).toBe(true);
    const second = toolSession(TAB)!;
    expect(second).not.toBe(first);
    closeTool(TAB);
    expect(second.isEnded).toBe(true);
    expect(toolSession(TAB)).toBeNull();
  });

  it("runs page operations one at a time, in order", async () => {
    const slow = slowPage();
    openTool(TAB, "annotate");
    const session = toolSession(TAB)!;
    const results: string[] = [];
    void session.run(op("first")).then((r) => results.push(r));
    // Made when its turn comes.
    void session.run((target) => op("second")(target)).then((r) => results.push(r));
    await flush();
    expect(slow.asked).toEqual(["first"]);
    await slow.answer("1");
    await flush();
    expect(slow.asked).toEqual(["first", "second"]);
    await slow.answer("2");
    await flush();
    expect(results).toEqual(["1", "2"]);
  });

  it("drops a reply that lands after the session ended", async () => {
    const slow = slowPage();
    openTool(TAB, "annotate");
    const session = toolSession(TAB)!;
    const late = session.run(op("hit"));
    await flush();
    closeTool(TAB);
    openTool(TAB, "annotate");
    await slow.answer("stale");
    await expect(late).rejects.toSatisfy(isSessionEnded);
    await expect(session.run(op("more"))).rejects.toSatisfy(isSessionEnded);
  });

  it("starts a tab's next session's page work after the last one's", async () => {
    const slow = slowPage();
    openTool(TAB, "annotate");
    const old = toolSession(TAB)!;
    void old.run(op("hit old")).catch(() => undefined);
    await flush();
    closeTool(TAB);
    openTool(TAB, "annotate");
    void toolSession(TAB)!.run(op("hit new"));
    await flush();
    expect(slow.asked).toEqual(["hit old"]);
    await slow.answer();
    await flush();
    expect(slow.asked).toEqual(["hit old", "hit new"]);
  });

  it("waits only so long for a last session whose page never answers", async () => {
    vi.useFakeTimers();
    const slow = slowPage();
    openTool(TAB, "annotate");
    void toolSession(TAB)!
      .run(op("hung"))
      .catch(() => undefined);
    await vi.advanceTimersByTimeAsync(0);
    closeTool(TAB);
    openTool(TAB, "annotate");
    void toolSession(TAB)!.run(op("hit new"));
    await vi.advanceTimersByTimeAsync(100);
    expect(slow.asked).toEqual(["hung"]);
    await vi.advanceTimersByTimeAsync(2000);
    expect(slow.asked).toEqual(["hung", "hit new"]);
  });

  it("refuses draft changes, phases and closing once it has ended", () => {
    openTool(TAB, "draw");
    const old = toolSession(TAB)!;
    closeTool(TAB);
    openTool(TAB, "draw");
    expect(old.edit({ note: "Stale" })).toBe(false);
    expect(old.begin("preparing")).toBe(false);
    old.close();
    expect(tabTools(TAB)).toMatchObject({ tool: "draw", note: "", phase: "editing" });
    const live = toolSession(TAB)!;
    expect(live.edit({ note: "Now" })).toBe(true);
    expect(live.begin("preparing")).toBe(true);
    expect(live.edit({ note: "Frozen" })).toBe(false);
    expect(live.settle()).toBe(true);
    expect(tabTools(TAB).note).toBe("Now");
  });

  it("removes its listeners when it ends", () => {
    openTool(TAB, "draw");
    const session = toolSession(TAB)!;
    const heard = vi.fn();
    session.listen(window, "pointerup", heard);
    window.dispatchEvent(new Event("pointerup"));
    expect(heard).toHaveBeenCalledTimes(1);
    closeTool(TAB);
    window.dispatchEvent(new Event("pointerup"));
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("says why, as the platform puts it, when the page can't run the tool", async () => {
    unavailable = "The page isn't ready yet.";
    openTool(TAB, "annotate");
    await expect(toolSession(TAB)!.page()).rejects.toThrow("The page isn't ready yet.");
  });
});
