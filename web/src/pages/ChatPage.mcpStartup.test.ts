// Vitest cases for the MCP startup band's pure line formatters.
import { describe, expect, it } from "vitest";

import { mcpSettledNames, mcpStartingLine } from "./ChatPage";

describe("mcpStartingLine", () => {
  it("mirrors the Codex TUI header: caps the name list at three", () => {
    // A 20-server config must stay one scannable line, not a paragraph.
    expect(mcpStartingLine(["a", "b", "c", "d", "e"], 20)).toBe(
      "Starting MCP servers (15/20): a, b, c, …",
    );
  });

  it("spells out short lists in full", () => {
    expect(mcpStartingLine(["glean", "safe"], 3)).toBe("Starting MCP servers (1/3): glean, safe");
  });

  it("uses the singular header for a single-server round", () => {
    expect(mcpStartingLine(["safe"], 1)).toBe("Starting MCP server: safe…");
  });
});

describe("mcpSettledNames", () => {
  it("keeps short failure lists verbatim", () => {
    expect(mcpSettledNames(["safe", "storage-console"])).toBe("safe, storage-console");
  });

  it("collapses long lists into a count so the band stays scannable", () => {
    const names = Array.from({ length: 12 }, (_, i) => `s${String(i).padStart(2, "0")}`);
    expect(mcpSettledNames(names)).toBe("s00, s01, s02, s03, s04, s05, s06, s07, +4 more");
  });
});
