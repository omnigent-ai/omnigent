import { afterEach, describe, expect, it, vi } from "vitest";
import type * as DesignOutboxModule from "./designOutbox";

const queued = vi.hoisted(() =>
  vi.fn((_request: { label: string; text: string; files?: File[] }) => "id"),
);
vi.mock("./designOutbox", async (importOriginal) => ({
  ...(await importOriginal<typeof DesignOutboxModule>()),
  queueDesignRequest: queued,
}));

import { setTabDesignPlatform, type DesignPage, type DesignPlatform } from "./designPlatform";
import {
  closeTool,
  editDrafts,
  openTool,
  resetDesignTools,
  tabTools,
  type CssRect,
  type Shape,
} from "./designToolsStore";
import {
  isSendingNotice,
  pickElement,
  SENDING,
  SENDING_WITHOUT_SCREENSHOT,
  sendAnnotation,
  sendDrawing,
} from "./toolActions";
import { toolSession } from "./toolSession";
import { parseDesignRequest } from "../parseDesignRequest";

// A platform whose page answers from `replies` and records each operation, and whose
// captures record what they were asked for.
const TAB = "browser-tab:conv_1:tab_1";
const replies: string[] = [];
const ran: string[] = [];
const screenshot = vi.fn(
  async (_tabId: string, _marks: readonly Shape[]) => new File(["png"], "d.png"),
);
const element = vi.fn(async (_tabId: string, _rect: CssRect) => new File(["png"], "element.png"));
const answer = async (done: string) => {
  ran.push(done);
  return replies.shift() ?? "";
};
const designPage: DesignPage = {
  hitTest: (x, y) => answer(`hit ${x},${y}`),
  scroll: () => answer("scroll"),
};
const platform: DesignPlatform = {
  page: async () => designPage,
  capture: { screenshot, element },
};
setTabDesignPlatform(TAB, platform);
const mark: Shape = {
  kind: "pen",
  points: [
    { x: 1, y: 2 },
    { x: 3, y: 4 },
  ],
  color: "#000000",
};
const session = () => toolSession(TAB)!;
const HIT = { element: { tag: "button", text: "Buy" }, rect: { x: 1, y: 2, width: 3, height: 4 } };
/** HIT as Annotate picks it: at the point it was clicked. */
const PICKED = { ...HIT, at: { x: 2, y: 3 } };
const flush = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

afterEach(() => {
  resetDesignTools();
  vi.useRealTimers();
  queued.mockClear();
  screenshot.mockClear();
  element.mockReset();
  element.mockImplementation(async () => new File(["png"], "element.png"));
  replies.length = 0;
  ran.length = 0;
});

describe("Annotate", () => {
  it("picks the element the page reports and sends a request", async () => {
    openTool(TAB, "annotate");
    replies.push(JSON.stringify(HIT));
    await pickElement(session(), 10, 20);
    expect(tabTools(TAB).picked).toEqual({ ...HIT, at: { x: 10, y: 20 } });
    editDrafts(TAB, { request: "Make it green" });
    replies.push(JSON.stringify(HIT));
    await expect(sendAnnotation(session())).resolves.toBe(SENDING);
    expect(queued).toHaveBeenCalledWith(
      expect.objectContaining({ label: 'Annotate button "Buy"', tabId: TAB }),
    );
    expect(queued.mock.calls[0]![0].text).toMatch(
      /^Make it green\n\n---\n\[Design Mode — modify this element in the browser preview\]/,
    );
    expect(tabTools(TAB).tool).toBeNull();
  });

  it("keeps the first pick when a second click's reply lands after it", async () => {
    openTool(TAB, "annotate");
    const hit = (tag: string) =>
      JSON.stringify({ element: { tag }, rect: { x: 1, y: 2, width: 3, height: 4 } });
    replies.push(hit("button"), hit("img"));
    // Both clicks are made before either reply.
    await Promise.all([pickElement(session(), 10, 20), pickElement(session(), 30, 40)]);
    expect(tabTools(TAB).picked?.element).toMatchObject({ tag: "button" });
  });

  it("ignores a reply that isn't a hit", async () => {
    openTool(TAB, "annotate");
    replies.push(JSON.stringify({ element: "nope" }));
    await pickElement(session(), 10, 20);
    expect(tabTools(TAB).picked).toBeNull();
  });

  it("sends nothing without a pick and a request", async () => {
    openTool(TAB, "annotate");
    await expect(sendAnnotation(session())).resolves.toBeNull();
    editDrafts(TAB, { picked: PICKED, request: "   " });
    await expect(sendAnnotation(session())).resolves.toBeNull();
    expect(queued).not.toHaveBeenCalled();
  });

  it("sends the element's screenshot, of where it is now", async () => {
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: PICKED, request: "Make it green" });
    // The page moved it since the pick: the screenshot is of where it is now.
    const moved = { x: 40, y: 50, width: 3, height: 4 };
    replies.push(JSON.stringify({ element: HIT.element, rect: moved }));
    const sending = sendAnnotation(session());
    // Frozen while it's taken.
    expect(tabTools(TAB).phase).toBe("preparing");
    expect(editDrafts(TAB, { request: "Changed" })).toBe(false);
    await expect(sending).resolves.toBe(SENDING);
    expect(ran).toEqual(["hit 2,3"]);
    expect(element).toHaveBeenCalledWith(TAB, moved);
    expect(queued.mock.calls[0]![0].files?.map((file) => file.name)).toEqual(["element.png"]);
    expect(queued.mock.calls[0]![0].text).not.toMatch(/Screenshot:/);
  });

  it("sends without the screenshot when it fails, and nothing once closed", async () => {
    let fail!: () => void;
    element.mockImplementation(
      () =>
        new Promise<File>((_resolve, reject) => {
          fail = () => reject(new Error("hidden"));
        }),
    );
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: PICKED, request: "Make it green" });
    replies.push(JSON.stringify(HIT), JSON.stringify(HIT));
    let sending = sendAnnotation(session());
    await flush();
    fail();
    await expect(sending).resolves.toBe(SENDING_WITHOUT_SCREENSHOT);
    expect(queued.mock.calls[0]![0].files).toBeUndefined();
    // The agent hears it's missing too, and the chat still reads the block as a request.
    const { text } = queued.mock.calls[0]![0];
    expect(text).toMatch(/\nScreenshot: none attached \(it couldn't be taken\)\n---$/);
    expect(parseDesignRequest(text)).toMatchObject({ kind: "element", request: "Make it green" });

    queued.mockClear();
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: PICKED, request: "Make it green" });
    sending = sendAnnotation(session());
    await flush();
    closeTool(TAB);
    fail();
    await expect(sending).resolves.toBeNull();
    expect(queued).not.toHaveBeenCalled();
  });

  it("sends without a screenshot when the page no longer has the element there", async () => {
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: PICKED, request: "Make it green" });
    // Something else is under the point now: a crop there would show the wrong thing.
    replies.push(JSON.stringify({ element: { tag: "img" }, rect: HIT.rect }));
    await expect(sendAnnotation(session())).resolves.toBe(SENDING_WITHOUT_SCREENSHOT);
    expect(element).not.toHaveBeenCalled();
    expect(queued.mock.calls[0]![0].files).toBeUndefined();
  });

  it("sends without a screenshot when the element there now is another alike", async () => {
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: { ...PICKED, key: "e1" }, request: "Make it green" });
    // The same description, but the page's key for it differs.
    replies.push(JSON.stringify({ ...HIT, key: "e2" }));
    await expect(sendAnnotation(session())).resolves.toBe(SENDING_WITHOUT_SCREENSHOT);
    expect(element).not.toHaveBeenCalled();
  });

  it("sends without a screenshot that never comes", async () => {
    vi.useFakeTimers();
    element.mockImplementation(() => new Promise<File>(() => {}));
    openTool(TAB, "annotate");
    editDrafts(TAB, { picked: PICKED, request: "Make it green" });
    replies.push(JSON.stringify(HIT));
    const sending = sendAnnotation(session());
    await vi.advanceTimersByTimeAsync(5000);
    await expect(sending).resolves.toBe(SENDING_WITHOUT_SCREENSHOT);
    expect(element).toHaveBeenCalled();
    expect(queued.mock.calls[0]![0].files).toBeUndefined();
  });
});

it("counts a send without the screenshot as sent", () => {
  expect(isSendingNotice(SENDING)).toBe(true);
  expect(isSendingNotice(SENDING_WITHOUT_SCREENSHOT)).toBe(true);
  expect(isSendingNotice("hidden")).toBe(false);
  expect(isSendingNotice(null)).toBe(false);
});

describe("Draw", () => {
  it("sends the platform's screenshot of the marks frozen at Send", async () => {
    openTool(TAB, "draw");
    editDrafts(TAB, { shapes: [mark], note: "Bigger" });
    expect(await sendDrawing(session(), "http://localhost:5173/")).toBe(SENDING);
    expect(screenshot).toHaveBeenCalledWith(TAB, [mark]);
    expect(queued.mock.calls[0]![0]).toMatchObject({ label: "Draw: Bigger" });
    expect(queued.mock.calls[0]![0].text).toContain("Page: http://localhost:5173\n");
    expect(queued.mock.calls[0]![0].files).toHaveLength(1);
    expect(tabTools(TAB).tool).toBeNull();
  });

  it("names its marks when there's no note", async () => {
    openTool(TAB, "draw");
    editDrafts(TAB, { shapes: [mark, mark] });
    await sendDrawing(session(), "http://localhost:5173/");
    expect(queued.mock.calls[0]![0]).toMatchObject({ label: "Draw: 2 marks" });
  });

  it("stays open, saying why, when the screenshot fails", async () => {
    openTool(TAB, "draw");
    editDrafts(TAB, { shapes: [mark] });
    screenshot.mockRejectedValueOnce(new Error("Couldn't capture the page."));
    expect(await sendDrawing(session(), "http://localhost:5173/")).toBe(
      "Couldn't capture the page.",
    );
    expect(tabTools(TAB)).toMatchObject({ tool: "draw", phase: "editing" });
    expect(queued).not.toHaveBeenCalled();
  });

  it("sends nothing when closed while capturing", async () => {
    let shoot!: () => void;
    screenshot.mockImplementationOnce(
      () =>
        new Promise<File>((resolve) => {
          shoot = () => resolve(new File(["png"], "d.png"));
        }),
    );
    openTool(TAB, "draw");
    editDrafts(TAB, { shapes: [mark] });
    const sending = sendDrawing(session(), "http://localhost:5173/");
    closeTool(TAB);
    shoot();
    await expect(sending).resolves.toBeNull();
    expect(queued).not.toHaveBeenCalled();
  });
});
