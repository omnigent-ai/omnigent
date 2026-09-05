import { afterEach, describe, expect, it } from "vitest";
import {
  normalizeTranscriptViewDefault,
  readTranscriptViewDefault,
  TRANSCRIPT_VIEW_DEFAULT,
  writeTranscriptViewDefault,
} from "./transcriptViewPreferences";

const STORAGE_KEY = "omnigent:default-transcript-view";

afterEach(() => {
  localStorage.clear();
});

describe("transcriptViewPreferences — read/write", () => {
  it("returns chat when nothing is stored", () => {
    expect(readTranscriptViewDefault()).toBe(TRANSCRIPT_VIEW_DEFAULT);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("stores terminal and clears the key for chat", () => {
    writeTranscriptViewDefault("terminal");
    expect(readTranscriptViewDefault()).toBe("terminal");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("terminal");

    writeTranscriptViewDefault("chat");
    expect(readTranscriptViewDefault()).toBe("chat");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });
});

describe("normalizeTranscriptViewDefault", () => {
  it("passes through valid values", () => {
    expect(normalizeTranscriptViewDefault("chat")).toBe("chat");
    expect(normalizeTranscriptViewDefault("terminal")).toBe("terminal");
  });

  it("maps unknown, null, and garbage to chat", () => {
    expect(normalizeTranscriptViewDefault("auto")).toBe("chat");
    expect(normalizeTranscriptViewDefault("bogus")).toBe("chat");
    expect(normalizeTranscriptViewDefault(null)).toBe("chat");
    expect(normalizeTranscriptViewDefault(undefined)).toBe("chat");
  });
});
