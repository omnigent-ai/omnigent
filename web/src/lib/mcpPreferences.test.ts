import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastMcpServices, writeLastMcpServices } from "./mcpPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("mcpPreferences", () => {
  it("returns an empty array when nothing is stored", () => {
    expect(readLastMcpServices()).toEqual([]);
  });

  it("round-trips a written selection", () => {
    writeLastMcpServices(["system.ai.github", "system.ai.slack"]);
    expect(readLastMcpServices()).toEqual(["system.ai.github", "system.ai.slack"]);
  });

  it("persists an empty selection distinctly from unset", () => {
    writeLastMcpServices(["system.ai.github"]);
    writeLastMcpServices([]);
    expect(readLastMcpServices()).toEqual([]);
  });

  it("overwrites the previous selection", () => {
    writeLastMcpServices(["system.ai.github"]);
    writeLastMcpServices(["system.ai.slack"]);
    expect(readLastMcpServices()).toEqual(["system.ai.slack"]);
  });

  it("treats a non-array JSON value as empty", () => {
    localStorage.setItem("omnigent:last-mcp-services", JSON.stringify({ foo: "bar" }));
    expect(readLastMcpServices()).toEqual([]);
  });

  it("treats malformed JSON as empty", () => {
    localStorage.setItem("omnigent:last-mcp-services", "not json");
    expect(readLastMcpServices()).toEqual([]);
  });

  it("filters out non-string entries", () => {
    localStorage.setItem(
      "omnigent:last-mcp-services",
      JSON.stringify(["system.ai.github", 42, null, "system.ai.slack"]),
    );
    expect(readLastMcpServices()).toEqual(["system.ai.github", "system.ai.slack"]);
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastMcpServices(["system.ai.github"])).not.toThrow();
    expect(readLastMcpServices()).toEqual([]);
  });
});
