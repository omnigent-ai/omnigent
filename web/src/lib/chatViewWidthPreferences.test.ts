import { afterEach, describe, expect, it } from "vitest";
import {
  applyChatViewWidth,
  CHAT_VIEW_WIDTH_DEFAULT,
  normalizeChatViewWidth,
  readChatViewWidth,
  writeChatViewWidth,
} from "./chatViewWidthPreferences";

const STORAGE_KEY = "omnigent:chat-view-width";

afterEach(() => {
  localStorage.clear();
  document.documentElement.style.removeProperty("--chat-column-max-width");
});

describe("chatViewWidthPreferences", () => {
  it("defaults to normal and clears the default value", () => {
    expect(readChatViewWidth()).toBe(CHAT_VIEW_WIDTH_DEFAULT);
    writeChatViewWidth("wide");
    expect(readChatViewWidth()).toBe("wide");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("wide");

    writeChatViewWidth("normal");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(document.documentElement.style.getPropertyValue("--chat-column-max-width")).toBe(
      "48rem",
    );
  });

  it("normalizes unknown values", () => {
    expect(normalizeChatViewWidth("wide")).toBe("wide");
    expect(normalizeChatViewWidth("bogus")).toBe("normal");
    expect(normalizeChatViewWidth(null)).toBe("normal");
  });

  it("applies the extra-wide CSS value", () => {
    applyChatViewWidth("extra-wide");
    expect(document.documentElement.style.getPropertyValue("--chat-column-max-width")).toBe(
      "64rem",
    );
  });
});
