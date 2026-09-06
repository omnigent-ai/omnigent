// Vitest cases for the MCP startup band's pure line formatter.
import { describe, expect, it } from "vitest";

import { mcpStartingLine } from "./ChatIndicators";

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
