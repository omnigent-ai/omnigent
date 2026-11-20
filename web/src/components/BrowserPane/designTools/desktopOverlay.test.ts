// DesignOverlay's openings: each start's opening, however its stop or the next start
// overtakes main's answers, is the one its requests and its stop name.
import { describe, expect, it, vi } from "vitest";

import type { Answer, DesignToolsBridge } from "./desktopBridge";
import { DesignOverlay } from "./desktopOverlay";

interface Call {
  op: string;
  args: Record<string, unknown>;
  answer: (value: Answer) => void;
}

/** A bridge whose answers the test gives, call by call. */
function heldBridge() {
  const calls: Call[] = [];
  const bridge: DesignToolsBridge = {
    browserDesignTools: ({ conversationId: _id, op, ...args }) =>
      new Promise<Answer>((answer) => {
        calls.push({ op, args, answer });
      }),
    onBrowserDesignToolsEvent: () => () => {},
  };
  const next = async (op: string) => {
    await vi.waitFor(() => expect(calls.find((call) => call.op === op)).toBeTruthy());
    const index = calls.findIndex((call) => call.op === op);
    return calls.splice(index, 1)[0]!;
  };
  return { bridge, calls, next };
}

/** Overlay windows: real same-origin documents (iframes), recorded by frame name. */
function frames() {
  const opened: { frame: string; win: Window }[] = [];
  const open = vi.fn((frame: string) => {
    const iframe = document.createElement("iframe");
    document.body.append(iframe);
    const win = iframe.contentWindow!;
    opened.push({ frame, win });
    return win;
  });
  return { open, opened };
}

const settle = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

describe("DesignOverlay", () => {
  it("asks main for nothing for a start stopped before it asked", async () => {
    const { bridge, calls } = heldBridge();
    const overlay = new DesignOverlay(bridge, "view_1", frames().open);
    overlay.start(true);
    overlay.stop();
    await settle();
    expect(calls).toEqual([]);
  });

  it("stops a start's opening that its stop overtook, and opens none for it", async () => {
    const { bridge, next } = heldBridge();
    const windows = frames();
    const overlay = new DesignOverlay(bridge, "view_1", windows.open);
    overlay.start(true);
    const first = await next("start");
    overlay.stop();
    overlay.start(true);
    first.answer({ ok: true, session: "s1", frame: "f1" });
    const stop = await next("stop");
    expect(stop.args).toEqual({ session: "s1" });
    stop.answer({ ok: true });
    (await next("start")).answer({ ok: true, session: "s2", frame: "f2" });
    await vi.waitFor(() => expect(overlay.current()).not.toBeNull());
    expect(windows.opened.map((opened) => opened.frame)).toEqual(["f2"]);
  });

  it("holds a request made while its opening starts, then sends it in that opening's name", async () => {
    const { bridge, next } = heldBridge();
    const overlay = new DesignOverlay(bridge, "view_1", frames().open);
    overlay.start(true);
    const pending = overlay.request("hitTest", { x: 1, y: 2 });
    (await next("start")).answer({ ok: true, session: "s1", frame: "f1" });
    const hit = await next("hitTest");
    expect(hit.args).toEqual({ session: "s1", x: 1, y: 2 });
    hit.answer({ ok: true, result: "{}" });
    await expect(pending).resolves.toEqual({ ok: true, result: "{}" });
  });

  it("refuses a request waiting for an opening that stopped with no start after it", async () => {
    const { bridge, calls, next } = heldBridge();
    const overlay = new DesignOverlay(bridge, "view_1", frames().open);
    overlay.start(true);
    const pending = overlay.request("hitTest", { x: 1, y: 2 });
    const refused = expect(pending).rejects.toThrow("The design tool isn't open on this page.");
    const start = await next("start");
    overlay.stop();
    start.answer({ ok: true, session: "s1", frame: "f1" });
    (await next("stop")).answer({ ok: true });
    await refused;
    await settle();
    expect(calls.map((call) => call.op)).toEqual([]);
    await expect(overlay.request("scroll")).rejects.toThrow("isn't open");
  });

  it("says a capture of an element off the page's screen needs it scrolled to", async () => {
    const { bridge, next } = heldBridge();
    const overlay = new DesignOverlay(bridge, "view_1", frames().open);
    overlay.start(true);
    const shot = overlay.capture({ x: 900, y: 10, width: 50, height: 20 });
    const refused = expect(shot).rejects.toThrow("The element isn't on screen.");
    (await next("start")).answer({ ok: true, session: "s1", frame: "f1" });
    (await next("capture")).answer({ ok: false, error: "nothing to capture there" });
    await refused;
  });

  it("stops main's opening when its window can't open, and says why", async () => {
    const { bridge, next } = heldBridge();
    const overlay = new DesignOverlay(bridge, "view_1", () => null);
    const failed = vi.fn();
    overlay.onFailed(failed);
    overlay.start(true);
    (await next("start")).answer({ ok: true, session: "s1", frame: "f1" });
    expect((await next("stop")).args).toEqual({ session: "s1" });
    expect(failed).toHaveBeenCalledWith("the tool's overlay didn't open");
    expect(overlay.current()).toBeNull();
  });

  it("tells the tools when its document goes without their stop", async () => {
    const { bridge, next } = heldBridge();
    const windows = frames();
    const overlay = new DesignOverlay(bridge, "view_1", windows.open);
    const lost = vi.fn();
    overlay.onLost(lost);
    overlay.start(true);
    (await next("start")).answer({ ok: true, session: "s1", frame: "f1" });
    await vi.waitFor(() => expect(overlay.current()).not.toBeNull());
    windows.opened[0]!.win.dispatchEvent(new Event("pagehide"));
    expect(lost).toHaveBeenCalledTimes(1);
    expect(overlay.current()).toBeNull();
    await expect(overlay.request("scroll")).rejects.toThrow("isn't open");
    // The tools' own stop afterwards still tells main.
    overlay.stop();
    expect((await next("stop")).args).toEqual({ session: "s1" });
  });
});
