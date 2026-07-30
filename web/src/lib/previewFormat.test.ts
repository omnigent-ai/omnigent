import { describe, expect, it } from "vitest";
import { formatPreview } from "./previewFormat";

describe("formatPreview — bare JSON", () => {
  it("pretty-prints valid JSON with two-space indent", () => {
    const out = formatPreview('{"command": "ls -la", "timeout": 5000}');
    expect(out).toBe('{\n  "command": "ls -la",\n  "timeout": 5000\n}');
  });

  it("pretty-prints a top-level array", () => {
    // `json.dumps` of a list produces a bare `[...]` preview, so the
    // `[`-dispatch branch must format too — not just objects.
    const out = formatPreview('[{"role": "user"}, {"role": "assistant"}]');
    expect(out).toBe('[\n  {\n    "role": "user"\n  },\n  {\n    "role": "assistant"\n  }\n]');
  });

  it("returns empty string for whitespace-only input", () => {
    expect(formatPreview("   \n  ")).toBe("");
  });

  it("passes plain text through unchanged", () => {
    // Free-form previews (shell commands, questions) must not be
    // mangled by the JSON paths.
    expect(formatPreview("rm -rf /tmp/cache")).toBe("rm -rf /tmp/cache");
    expect(formatPreview("Proceed (or not)?")).toBe("Proceed (or not)?");
  });

  it("reindents truncated JSON instead of falling back to one line", () => {
    // The server caps content_preview at 1024 chars, so JSON often
    // arrives cut off mid-token and JSON.parse fails. The reindenter
    // must still break it across lines — the old behavior (raw
    // single-line fallback) is exactly the unreadable case this
    // module exists to fix.
    const truncated = '{"file_path": "/tmp/a.txt", "content": "abc';
    const out = formatPreview(truncated);
    expect(out).toBe('{\n  "file_path": "/tmp/a.txt",\n  "content": "abc');
  });

  it("does not break lines on structural characters inside strings", () => {
    // Braces/commas/brackets inside string values are content, not
    // structure. A scanner that ignores string state would explode
    // this onto bogus lines.
    const out = formatPreview('{"code": "if (a) { b(x, y); }"');
    expect(out).toBe('{\n  "code": "if (a) { b(x, y); }"');
  });

  it("tracks escaped quotes when reindenting", () => {
    // `\"` must not terminate the string state — if it did, the `,`
    // after it would be treated as structural and get a line break.
    const out = formatPreview('{"msg": "say \\"hi\\", then stop"');
    expect(out).toBe('{\n  "msg": "say \\"hi\\", then stop"');
  });

  it("keeps empty containers compact when reindenting", () => {
    // `{}` / `[]` should not become a `{`-newline-`}` sandwich.
    const out = formatPreview('{"args": [], "env": {}, "x": 1');
    expect(out).toBe('{\n  "args": [],\n  "env": {},\n  "x": 1');
  });

  it("indents nested structures by depth when reindenting", () => {
    const out = formatPreview('{"a": {"b": [1, 2');
    expect(out).toBe('{\n  "a": {\n    "b": [\n      1,\n      2');
  });
});

describe("formatPreview — ToolName({...}) wrapper", () => {
  it("pretty-prints the JSON body of an intact tool-call preview", () => {
    // The claude-native / claude-sdk permission hooks emit
    // `f"{tool_name}({json})"` — the name must survive and the body
    // must be indented like bare JSON.
    const out = formatPreview('Bash({"command": "ls", "timeout": 5})');
    expect(out).toBe('Bash({\n  "command": "ls",\n  "timeout": 5\n})');
  });

  it("formats a truncated body without inventing a closing paren", () => {
    // The 1024-char cap can eat the trailing `)` (and part of the
    // JSON). The output must stay verbatim-truncated — appending a
    // `)` would misrepresent what the server sent.
    const out = formatPreview('Write({"file_path": "/tmp/a", "content": "abc');
    expect(out).toBe('Write({\n  "file_path": "/tmp/a",\n  "content": "abc');
    expect(out.endsWith(")")).toBe(false);
  });

  it("keeps empty-args tool calls compact", () => {
    // `Edit({})` is a real producer shape (no args yet); a multi-line
    // render of an empty object is pure noise.
    expect(formatPreview("Edit({})")).toBe("Edit({})");
  });

  it("supports MCP-style tool names with dashes and underscores", () => {
    const out = formatPreview('mcp__chrome-devtools__click({"uid": "n1"})');
    expect(out).toBe('mcp__chrome-devtools__click({\n  "uid": "n1"\n})');
  });

  it("pretty-prints a tool call whose body is an array", () => {
    // Rare but reachable: a tool input serialized as a JSON array
    // rather than an object. The body-dispatch must accept `[` too.
    const out = formatPreview('TodoWrite(["buy milk", "walk dog"])');
    expect(out).toBe('TodoWrite([\n  "buy milk",\n  "walk dog"\n])');
  });

  it("leaves call-shaped text with a non-JSON body unchanged", () => {
    // e.g. the AskUserQuestion fallback card can receive garbage
    // inside the parens; rewriting it would only obscure the raw
    // payload the user needs to see.
    const garbage = "AskUserQuestion(<TRUNCATED-GARBAGE>";
    expect(formatPreview(garbage)).toBe(garbage);
  });
});
