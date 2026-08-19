import { afterEach, describe, expect, it } from "vitest";
import {
  normalizeDefaultSessionView,
  readDefaultSessionView,
  SESSION_VIEW_DEFAULT,
  writeDefaultSessionView,
} from "./sessionViewPreferences";

const STORAGE_KEY = "omnigent:default-session-view";

afterEach(() => {
  localStorage.clear();
});

describe("sessionViewPreferences — read/write", () => {
  it("preserves the current chat default when nothing is stored", () => {
    expect(readDefaultSessionView()).toBe(SESSION_VIEW_DEFAULT);
    expect(readDefaultSessionView()).toBe("chat");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("persists terminal and clears the key for chat", () => {
    writeDefaultSessionView("terminal");
    expect(readDefaultSessionView()).toBe("terminal");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("terminal");

    writeDefaultSessionView("chat");
    expect(readDefaultSessionView()).toBe("chat");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("normalizeDefaultSessionView", () => {
  it("passes through supported values", () => {
    expect(normalizeDefaultSessionView("chat")).toBe("chat");
    expect(normalizeDefaultSessionView("terminal")).toBe("terminal");
  });

  it("maps missing and unknown values to chat", () => {
    expect(normalizeDefaultSessionView(null)).toBe("chat");
    expect(normalizeDefaultSessionView(undefined)).toBe("chat");
    expect(normalizeDefaultSessionView("shell")).toBe("chat");
  });
});
