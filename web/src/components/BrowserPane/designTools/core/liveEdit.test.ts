import { afterEach, describe, expect, it, vi } from "vitest";
import type * as DesignOutboxModule from "./designOutbox";

const queued = vi.hoisted(() => vi.fn((_request: { label: string; text: string }) => "id"));
vi.mock("./designOutbox", async (importOriginal) => ({
  ...(await importOriginal<typeof DesignOutboxModule>()),
  queueDesignRequest: queued,
}));

import { setTabDesignPlatform, type DesignPage } from "./designPlatform";
import { closeTool, openTool, resetDesignTools, tabTools } from "./designToolsStore";
import { LIVE_EDIT_MAX_TEXT, liveEditFor } from "./liveEdit";
import { SENDING } from "./toolActions";
import { toolSession } from "./toolSession";
import { parseDesignRequest } from "../parseDesignRequest";

// A page that answers from `replies` and records each operation.
const TAB = "browser-tab:conv_1:tab_1";
const replies: string[] = [];
const ran: string[] = [];
const answer = async (done: string) => {
  ran.push(done);
  return replies.shift() ?? "";
};
const designPage: DesignPage = {
  hitTest: () => answer("hit"),
  scroll: () => answer("scroll"),
  liveEditPick: (_scope, x, y) => answer(`pick ${x},${y}`),
  // A preview answers as the page does: `ok`, unless the test says otherwise.
  liveEditPreview: async (_scope, handle, text) =>
    (await answer(`preview ${handle} ${JSON.stringify(text)}`)) || "ok",
  liveEditRestore: () => answer("restore"),
  liveEditSettle: (_scope, texts) => answer(`settle ${JSON.stringify(texts)}`),
  liveEditForget: () => answer("forget"),
  liveEditDiscard: () => answer("discard"),
};
setTabDesignPlatform(TAB, {
  page: async () => designPage,
  capture: {
    screenshot: async () => new File([], "x.png"),
    element: async () => new File([], "x.png"),
  },
});

/** A pick's reply. */
const picked = (handle: number, text: string, extra: Record<string, unknown> = {}) =>
  JSON.stringify({
    handle,
    before: text,
    text,
    element: { tag: "p", id: "#title" },
    rect: { x: 0, y: 0, width: 10, height: 10 },
    ...extra,
  });
const flush = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
/** Live Edit open on the tab, with its actions. */
const openLiveEdit = (onNotice = vi.fn()) => {
  openTool(TAB, "edit");
  return liveEditFor(toolSession(TAB)!, onNotice);
};

afterEach(() => {
  resetDesignTools();
  queued.mockClear();
  replies.length = 0;
  ran.length = 0;
});

describe("Live Edit", () => {
  it("previews, sends the changed edits, then settles the page to the app's text", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    expect(tabTools(TAB)).toMatchObject({ editing: 1, edits: [{ before: "Old", after: "Old" }] });
    live.type(1, "New");
    await flush();
    expect(ran).toContain('preview 1 "New"');
    expect(live.send("https://app.example.com/orders?token=t0k3n")).toBe(SENDING);
    const request = queued.mock.calls[0]![0];
    expect(request.label).toBe("Live Edit: 1 edit");
    expect(request.text).toContain("Page: https://app.example.com\n");
    expect(request.text).not.toContain("t0k3n");
    expect(request.text).toContain('1. <p>#title: "Old" → "New"');
    expect(parseDesignRequest(request.text)).toMatchObject({
      kind: "edits",
      edits: [{ element: "p#title", before: "Old", after: "New" }],
    });
    expect(tabTools(TAB).tool).toBeNull();
    await flush();
    expect(ran.at(-1)).toBe('settle [[1,"New"]]');
  });

  it("sends nothing until some text changed", async () => {
    const live = openLiveEdit();
    expect(live.send("https://app.example.com/")).toBeNull();
    replies.push(picked(1, "Same"));
    await live.pick(5, 5);
    live.type(1, "Same");
    expect(live.send("https://app.example.com/")).toBeNull();
    expect(queued).not.toHaveBeenCalled();
    expect(tabTools(TAB).tool).toBe("edit");
  });

  it("opens the editor for the latest pick only, so typing after it is kept", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "First"), picked(2, "Second"));
    const first = live.pick(5, 5);
    const second = live.pick(6, 6);
    await first;
    expect(tabTools(TAB).editing).toBeNull();
    await second;
    expect(tabTools(TAB).editing).toBe(2);
    live.type(2, "Typed");
    expect(tabTools(TAB).edits.map((edit) => [edit.handle, edit.after])).toEqual([[2, "Typed"]]);
  });

  it("closes the editor before a pick, and keeps an edit when it's picked again", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    live.type(1, "Typed");
    await flush();
    replies.push(picked(1, "Typed"));
    const picking = live.pick(1, 1);
    expect(tabTools(TAB).editing).toBeNull();
    await picking;
    expect(tabTools(TAB)).toMatchObject({ editing: 1, edits: [{ before: "Old", after: "Typed" }] });
  });

  it("keeps where in the text a pick landed, if it's in the text", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old", { caret: 2 }));
    await live.pick(5, 5);
    expect(tabTools(TAB)).toMatchObject({ editing: 1, caret: 2 });
    // Off the text, past it, or not a position: the editor starts at the end.
    for (const caret of [-1, 4, 1.5, "2"]) {
      replies.push(picked(1, "Old", { caret }));
      // oxlint-disable-next-line no-await-in-loop -- one pick at a time
      await live.pick(5, 5);
      expect(tabTools(TAB)).toMatchObject({ editing: 1, caret: null });
    }
  });

  it("says why the page refused a pick, and opens nothing", async () => {
    const onNotice = vi.fn();
    const live = openLiveEdit(onNotice);
    for (const [refusal, notice] of [
      ["nested", "Live Edit changes plain text only"],
      ["busy", "The last Live Edit is still finishing on that text."],
      ["tooLong", "That text is too long to edit here."],
      ["tooMany", "That's as many edits as one Live Edit sends."],
    ] as const) {
      replies.push(JSON.stringify({ [refusal]: true }));
      // oxlint-disable-next-line no-await-in-loop -- one pick at a time
      await live.pick(1, 1);
      expect(onNotice).toHaveBeenLastCalledWith(expect.stringContaining(notice));
    }
    expect(tabTools(TAB)).toMatchObject({ editing: null, edits: [] });
  });

  it("says when the text left the page, and doesn't settle it", async () => {
    const onNotice = vi.fn();
    const live = openLiveEdit(onNotice);
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    replies.push("gone");
    live.type(1, "New");
    await flush();
    expect(onNotice).toHaveBeenCalledWith("That text isn't on the page any more.");
    closeTool(TAB);
    await flush();
    expect(ran.at(-1)).toBe("settle []");
  });

  it("previews only the newest text of what was typed while a preview ran", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    live.type(1, "A");
    live.type(1, "AB");
    live.type(1, "ABC");
    await flush();
    expect(ran.filter((op) => op.startsWith("preview"))).toEqual(['preview 1 "ABC"']);
  });

  it("puts the page back on Discard and closes once it's back", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    live.type(1, "New");
    replies.push("", "1");
    await live.discard();
    expect(ran).toContain("restore");
    expect(tabTools(TAB).tool).toBeNull();
    await flush();
    expect(ran.at(-1)).toBe("forget");
  });

  it("stays open to try again when the page couldn't be put back", async () => {
    const onNotice = vi.fn();
    const live = openLiveEdit(onNotice);
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    replies.push("none");
    await live.discard();
    expect(tabTools(TAB)).toMatchObject({ tool: "edit", phase: "editing" });
    expect(onNotice).toHaveBeenCalledWith(
      "Couldn't put the original text back. Try Discard again.",
    );
  });

  it("puts the page back when it's closed during Discard, instead of keeping the edits", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    live.type(1, "New");
    void live.discard();
    // Another tool replaces it before Discard's restore has its turn.
    openTool(TAB, "annotate");
    await flush();
    expect(ran).not.toContain("restore");
    expect(ran.at(-1)).toBe("discard");
  });

  it("settles the edits typed in most recently last", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "One"));
    await live.pick(5, 5);
    replies.push(picked(2, "Two"));
    await live.pick(6, 6);
    live.type(1, "One!");
    live.type(2, "Two!");
    live.type(1, "One!!");
    await flush();
    closeTool(TAB);
    await flush();
    expect(ran.at(-1)).toBe('settle [[2,"Two!"],[1,"One!!"]]');
  });

  it("keeps an edit's text within what the page takes", async () => {
    const live = openLiveEdit();
    replies.push(picked(1, "Old"));
    await live.pick(5, 5);
    live.type(1, "x".repeat(LIVE_EDIT_MAX_TEXT + 5));
    expect(tabTools(TAB).edits[0]!.after).toHaveLength(LIVE_EDIT_MAX_TEXT);
  });

  it("ignores a pick's reply that isn't one", async () => {
    const live = openLiveEdit();
    replies.push(JSON.stringify({ handle: "1", before: "x", text: "x" }));
    await live.pick(5, 5);
    replies.push("null");
    await live.pick(5, 5);
    expect(tabTools(TAB)).toMatchObject({ editing: null, edits: [] });
  });
});
