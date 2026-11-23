import { describe, expect, it } from "vitest";
import {
  decodeFrame,
  fitPage,
  isRetryableClose,
  keyMessage,
  modifierMask,
  mouseButtonName,
  pagePoint,
  parseServerMessage,
  chromeBrowserAttachPath,
} from "./chromeBrowserProtocol";

function encodeFrame(header: unknown, jpeg: number[]): ArrayBuffer {
  const headerBytes = new TextEncoder().encode(JSON.stringify(header));
  const bytes = new Uint8Array(4 + headerBytes.length + jpeg.length);
  new DataView(bytes.buffer).setUint32(0, headerBytes.length);
  bytes.set(headerBytes, 4);
  bytes.set(jpeg, 4 + headerBytes.length);
  return bytes.buffer;
}

const NO_MODIFIERS = { altKey: false, ctrlKey: false, metaKey: false, shiftKey: false };

describe("chromeBrowserAttachPath", () => {
  it("adds only the parameters that are set", () => {
    expect(chromeBrowserAttachPath("conv 1", false)).toBe(
      "/v1/sessions/conv%201/resources/chrome_browser/attach",
    );
    expect(chromeBrowserAttachPath("conv_1", true, "host_9")).toBe(
      "/v1/sessions/conv_1/resources/chrome_browser/attach?read_only=true&omnigent_slice_key=host_9",
    );
  });
});

describe("decodeFrame", () => {
  it("splits the header from the image", () => {
    const frame = decodeFrame(encodeFrame({ seq: 3, width: 1280, height: 800 }, [255, 216, 7]));
    expect(frame).toMatchObject({ seq: 3, width: 1280, height: 800 });
    expect(Array.from(frame!.jpeg)).toEqual([255, 216, 7]);
  });

  it("rejects truncated, oversized-header and non-numeric frames", () => {
    expect(decodeFrame(new ArrayBuffer(2))).toBeNull();
    const lying = new Uint8Array(8);
    new DataView(lying.buffer).setUint32(0, 400);
    expect(decodeFrame(lying.buffer)).toBeNull();
    expect(decodeFrame(encodeFrame({ seq: "3", width: 1, height: 1 }, []))).toBeNull();
  });
});

describe("parseServerMessage", () => {
  it("reads state with defaults for missing fields", () => {
    expect(
      parseServerMessage(
        JSON.stringify({
          type: "state",
          url: "https://example.com/",
          title: "Home",
          can_go_back: true,
          tab_id: "t1",
          tabs: [{ id: "t1", title: "Home", url: "https://example.com/" }, { title: "no id" }],
        }),
      ),
    ).toEqual({
      type: "state",
      state: {
        url: "https://example.com/",
        title: "Home",
        canGoBack: true,
        canGoForward: false,
        loading: false,
        tabId: "t1",
        tabs: [{ id: "t1", title: "Home", url: "https://example.com/" }],
      },
    });
  });

  it("reads ready and notice, and ignores the rest", () => {
    expect(parseServerMessage('{"type":"ready","read_only":true}')).toEqual({
      type: "ready",
      readOnly: true,
    });
    expect(parseServerMessage('{"type":"notice","message":"hi"}')).toEqual({
      type: "notice",
      message: "hi",
    });
    expect(parseServerMessage('{"type":"future"}')).toBeNull();
    expect(parseServerMessage("not json")).toBeNull();
  });
});

describe("isRetryableClose", () => {
  it("gives up only when the host has no browser or the viewer is refused", () => {
    expect(isRetryableClose(4404)).toBe(false);
    expect(isRetryableClose(1008)).toBe(false);
    expect(isRetryableClose(4405)).toBe(true);
    expect(isRetryableClose(4502)).toBe(true);
    expect(isRetryableClose(1006)).toBe(true);
  });
});

describe("pointer mapping", () => {
  it("fits the page inside the stage keeping its shape", () => {
    expect(fitPage({ width: 1000, height: 500 }, { width: 500, height: 500 })).toEqual({
      width: 500,
      height: 250,
    });
    expect(fitPage({ width: 0, height: 500 }, { width: 500, height: 500 })).toEqual({
      width: 0,
      height: 0,
    });
  });

  it("maps a pointer on the drawn canvas to page pixels", () => {
    const drawn = { left: 100, top: 50, width: 500, height: 250 };
    expect(pagePoint({ x: 350, y: 175 }, drawn, { width: 1000, height: 500 })).toEqual({
      x: 500,
      y: 250,
    });
  });

  it("names buttons and packs modifiers the way DevTools expects", () => {
    expect([0, 1, 2, 3].map(mouseButtonName)).toEqual(["left", "middle", "right", "none"]);
    expect(modifierMask({ ...NO_MODIFIERS, ctrlKey: true, shiftKey: true })).toBe(10);
    expect(modifierMask({ ...NO_MODIFIERS, altKey: true, metaKey: true })).toBe(5);
  });
});

describe("keyMessage", () => {
  it("types a character on press but not on release", () => {
    const key = { ...NO_MODIFIERS, key: "a", code: "KeyA", keyCode: 65 };
    expect(keyMessage(key, "down")).toMatchObject({ event: "down", text: "a", key_code: 65 });
    expect(keyMessage(key, "up")).not.toHaveProperty("text");
  });

  it("sends Enter as a carriage return and shortcuts without text", () => {
    expect(
      keyMessage({ ...NO_MODIFIERS, key: "Enter", code: "Enter", keyCode: 13 }, "down").text,
    ).toBe("\r");
    const selectAll = { ...NO_MODIFIERS, ctrlKey: true, key: "a", code: "KeyA", keyCode: 65 };
    expect(keyMessage(selectAll, "down")).not.toHaveProperty("text");
    expect(
      keyMessage({ ...NO_MODIFIERS, key: "ArrowUp", code: "ArrowUp", keyCode: 38 }, "down"),
    ).not.toHaveProperty("text");
  });
});
