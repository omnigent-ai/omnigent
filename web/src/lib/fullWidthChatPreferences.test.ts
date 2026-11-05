import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyFullWidthChat,
  DEFAULT_FULL_WIDTH_CHAT,
  readFullWidthChat,
  writeFullWidthChat,
} from "./fullWidthChatPreferences";

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.fullWidthChat;
  vi.restoreAllMocks();
});

describe("fullWidthChatPreferences", () => {
  it("defaults to off when nothing is stored", () => {
    expect(DEFAULT_FULL_WIDTH_CHAT).toBe(false);
    expect(readFullWidthChat()).toBe(false);
  });

  it("round-trips both boolean values", () => {
    writeFullWidthChat(true);
    expect(readFullWidthChat()).toBe(true);

    writeFullWidthChat(false);
    expect(readFullWidthChat()).toBe(false);
  });

  it('treats any non-"true" stored value as off', () => {
    localStorage.setItem("omnigent:full-width-chat", "1");
    expect(readFullWidthChat()).toBe(false);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeFullWidthChat(true)).not.toThrow();
    expect(readFullWidthChat()).toBe(false);
  });

  it("toggles the <html> attribute the CSS keys off", () => {
    applyFullWidthChat(true);
    expect(document.documentElement).toHaveAttribute("data-full-width-chat", "true");

    applyFullWidthChat(false);
    expect(document.documentElement).not.toHaveAttribute("data-full-width-chat");
  });
});
