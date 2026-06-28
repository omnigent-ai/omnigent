import { afterEach, describe, expect, it } from "vitest";
import { DEFAULT_CHAT_FONT, applyChatFont, readChatFont, writeChatFont } from "./chatFont";

const STORAGE_KEY = "omnigent:chat-font";

afterEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-chat-font");
});

describe("chatFont", () => {
  it("returns the default when nothing is stored", () => {
    // No write has happened — read must fall back to the system default,
    // not throw or return an unknown value.
    expect(readChatFont()).toBe(DEFAULT_CHAT_FONT);
  });

  it("round-trips a written font", () => {
    writeChatFont("geist");
    expect(readChatFont()).toBe("geist");
  });

  it("falls back to the default on an unrecognized stored value", () => {
    // A stale or hand-edited value must not leak through to CSS; read coerces
    // it back to the default so the attribute logic stays well-defined.
    localStorage.setItem(STORAGE_KEY, "comic-sans");
    expect(readChatFont()).toBe(DEFAULT_CHAT_FONT);
  });

  it("sets data-chat-font on the root for a non-default font", () => {
    applyChatFont("geist");
    expect(document.documentElement.getAttribute("data-chat-font")).toBe("geist");
  });

  it("clears the attribute for the default font", () => {
    // Start dirty, then apply the default — the attribute must be removed so
    // the base font-sans stack applies rather than an empty/garbage value.
    document.documentElement.setAttribute("data-chat-font", "geist");
    applyChatFont(DEFAULT_CHAT_FONT);
    expect(document.documentElement.hasAttribute("data-chat-font")).toBe(false);
  });

  it("applies the font as a side effect of writing", () => {
    writeChatFont("geist");
    expect(document.documentElement.getAttribute("data-chat-font")).toBe("geist");
    writeChatFont("system");
    expect(document.documentElement.hasAttribute("data-chat-font")).toBe(false);
  });
});
