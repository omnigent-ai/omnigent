import { describe, expect, it } from "vitest";
import type { ToolCall } from "./toolResultSummary";
import { imageReadPath, inlineOutputImages, toolResultImages } from "./toolResultImages";

const PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const PNG_URI = `data:image/png;base64,${PNG}`;

function call(overrides: Partial<ToolCall>): ToolCall {
  return { name: "Read", args: {}, output: "ok", state: "output-available", ...overrides };
}

describe("imageReadPath", () => {
  it.each([
    ["Read", { file_path: "/ws/shot.png" }, "/ws/shot.png"],
    ["sys_os_read", { path: "shots/a.JPG" }, "shots/a.JPG"],
    ["mcp__omnigent__sys_os_read", { path: "a.webp" }, "a.webp"],
    ["read", { filePath: "/ws/a.gif" }, "/ws/a.gif"],
    ["read", { path: "a.jpeg" }, "a.jpeg"],
    ["view_image", { path: "/tmp/x.avif" }, "/tmp/x.avif"],
  ])("finds the image path %s reads", (name, args, expected) => {
    expect(imageReadPath(name, args)).toBe(expected);
  });

  it.each([
    ["a non-image file", "Read", { file_path: "/ws/notes.md" }],
    ["an SVG, which could run scripts once opened", "Read", { file_path: "/ws/logo.svg" }],
    ["an extension only in the middle", "Read", { file_path: "/ws/a.png.txt" }],
    ["a missing path", "Read", {}],
    ["a non-string path", "sys_os_read", { path: 3 }],
    ["the wrong argument key", "Read", { path: "/ws/a.png" }],
    ["a third-party MCP server's read", "mcp__files__read", { path: "a.png" }],
    ["a write tool", "Write", { file_path: "/ws/a.png" }],
  ])("ignores %s", (_label, name, args) => {
    expect(imageReadPath(name, args)).toBeNull();
  });
});

describe("inlineOutputImages", () => {
  const mcpBlock = { type: "image", data: PNG, mimeType: "image/png" };

  it.each([
    ["the tagged MCP envelope", { __omnigent_mcp_image_result__: true, content: [mcpBlock] }],
    ["a content result", { content: [{ type: "text", text: "shot" }, mcpBlock] }],
    ["a bare block array", [mcpBlock]],
    ["a lone image block", mcpBlock],
    [
      "an Anthropic source block",
      { type: "image", source: { type: "base64", media_type: "image/PNG", data: PNG } },
    ],
  ])("reads %s", (_label, value) => {
    expect(inlineOutputImages(JSON.stringify(value))).toEqual([
      { kind: "inline", src: PNG_URI, alt: "Tool result image" },
    ]);
  });

  it("reads newline-joined text and image lines, numbering several images", () => {
    const jpeg = { type: "image", data: "/9j/4AAQ", mimeType: "image/jpeg" };
    const output = ["Captured:", JSON.stringify(mcpBlock), JSON.stringify(jpeg)].join("\n");
    expect(inlineOutputImages(output)).toEqual([
      { kind: "inline", src: PNG_URI, alt: "Tool result image 1" },
      { kind: "inline", src: "data:image/jpeg;base64,/9j/4AAQ", alt: "Tool result image 2" },
    ]);
  });

  it("shows a repeated image once", () => {
    expect(inlineOutputImages(JSON.stringify([mcpBlock, mcpBlock]))).toEqual([
      { kind: "inline", src: PNG_URI, alt: "Tool result image" },
    ]);
  });

  it("strips whitespace wrapped into the base64 payload", () => {
    const wrapped = { ...mcpBlock, data: `${PNG.slice(0, 20)}\n${PNG.slice(20)}` };
    expect(inlineOutputImages(JSON.stringify(wrapped))[0]).toMatchObject({ src: PNG_URI });
  });

  it.each([
    ["plain text", "Screenshot saved to /ws/a.png"],
    ["invalid base64", JSON.stringify({ ...mcpBlock, data: "not base64!" })],
    ["empty data", JSON.stringify({ ...mcpBlock, data: "" })],
    ["an SVG block", JSON.stringify({ ...mcpBlock, mimeType: "image/svg+xml" })],
    ["a URL source", JSON.stringify({ type: "image", source: { type: "url", url: "https://x" } })],
    ["a text block", JSON.stringify({ type: "text", text: "hi" })],
  ])("finds nothing in %s", (_label, output) => {
    expect(inlineOutputImages(output)).toEqual([]);
  });
});

describe("toolResultImages", () => {
  it("returns the file a read tool opened", () => {
    expect(toolResultImages(call({ args: { file_path: "/ws/shots/home.png" } }))).toEqual([
      { kind: "file", path: "/ws/shots/home.png", alt: "home.png" },
    ]);
  });

  it("prefers images the output still carries over refetching the file", () => {
    const output = JSON.stringify([{ type: "image", data: PNG, mimeType: "image/png" }]);
    expect(toolResultImages(call({ args: { file_path: "/ws/a.png" }, output }))).toEqual([
      { kind: "inline", src: PNG_URI, alt: "Tool result image" },
    ]);
  });

  it("shows output images for any tool, not only reads", () => {
    const output = JSON.stringify({
      content: [{ type: "image", data: PNG, mimeType: "image/png" }],
    });
    expect(toolResultImages(call({ name: "mcp__browser__screenshot", output }))).toHaveLength(1);
  });

  it.each<[string, Partial<ToolCall>]>([
    ["a running call", { state: "input-available", output: null }],
    ["a call without output", { output: null }],
    ["a failed read", { output: "<tool_use_error>File does not exist.</tool_use_error>" }],
    [
      "an OpenCode read that reports an error",
      { name: "read", args: { filePath: "/ws/missing.png" }, output: "[error] File not found" },
    ],
    ["a JSON error", { output: JSON.stringify({ error: "denied" }) }],
    ["an errored state", { state: "output-error", output: "boom" }],
  ])("shows nothing for %s", (_label, overrides) => {
    expect(toolResultImages(call({ args: { file_path: "/ws/a.png" }, ...overrides }))).toEqual([]);
  });
});
