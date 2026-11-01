// Images a finished tool call read or returned, for the opt-in previews under
// its row: image blocks still carried by the stored output, or else the image
// file a read tool opened, fetched from the workspace by its path.

import type { ToolCall } from "./toolResultSummary";
import { splitToolName } from "./toolTitle";
import { editErrorMessage, isRecord, parseToolOutput, toolErrorMessage } from "./toolValue";

export type ToolResultImage =
  /** Bytes the stored output carries, as a `data:image/...` URI. */
  | { kind: "inline"; src: string; alt: string }
  /** An image file the call read, as the path the agent passed. */
  | { kind: "file"; path: string; alt: string };

/**
 * Argument holding the file path, by read tool: Claude `Read`, Omnigent
 * `sys_os_read`, Pi/OpenCode `read` (`path` / `filePath`), Codex `view_image`.
 */
const IMAGE_READ_PATH_ARGS: Readonly<Record<string, readonly string[]>> = {
  Read: ["file_path"],
  sys_os_read: ["path"],
  read: ["filePath", "path"],
  view_image: ["path"],
};

// SVG is left out: its app-origin blob URL would run scripts if opened as a document.
const IMAGE_FILE_RE = /\.(?:png|jpe?g|gif|webp|bmp|avif)$/i;

const RASTER_IMAGE_MEDIA_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/bmp",
  "image/avif",
]);

/** Whether `mediaType` (e.g. a served file's content type) is a raster image safe to preview. */
export function isRasterImageType(mediaType: string | null | undefined): boolean {
  const essence = mediaType?.split(";")[0]?.trim().toLowerCase();
  return essence !== undefined && RASTER_IMAGE_MEDIA_TYPES.has(essence);
}

const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/;

/**
 * The images to preview for a tool call, or none while it runs or when it failed.
 *
 * Output-carried images win: a harness that keeps the bytes needs no fetch.
 * Otherwise a read tool whose path names an image file yields that file.
 *
 * :param call: The tool call, e.g. `{ name: "Read", args: { file_path: "/ws/shot.png" }, ... }`.
 * :returns: Previews in output order; empty when there is nothing to show.
 */
export function toolResultImages(call: ToolCall): ToolResultImage[] {
  const { output, state } = call;
  if (state !== "output-available" || output === null) return [];
  if (toolErrorMessage(parseToolOutput(output)) !== null) return [];
  const inline = inlineOutputImages(output);
  if (inline.length > 0) return inline;
  const path = imageReadPath(call.name, call.args);
  // Read tools never echo arbitrary text, so the "[error] <message>" form counts too.
  if (path === null || editErrorMessage(output) !== null) return [];
  return [{ kind: "file", path, alt: baseName(path) }];
}

/** The image file path a read tool opened, or `null` for any other call. */
export function imageReadPath(name: string, args: Record<string, unknown>): string | null {
  const { server, tool } = splitToolName(name);
  if (server !== undefined || !Object.hasOwn(IMAGE_READ_PATH_ARGS, tool)) return null;
  for (const key of IMAGE_READ_PATH_ARGS[tool]!) {
    const value = args[key];
    if (typeof value === "string" && IMAGE_FILE_RE.test(value)) return value;
  }
  return null;
}

/**
 * Distinct data URIs for the image blocks in a stored tool output: a `{"content": [...]}`
 * result (including Omnigent's tagged MCP envelope), a bare block array, a lone
 * image object, or text lines interleaved with image-object lines.
 */
export function inlineOutputImages(output: string): ToolResultImage[] {
  const blocks = outputBlocks(output);
  // A repeated image adds nothing, and each source keys its preview.
  const sources = [
    ...new Set(
      blocks.flatMap((block) => {
        const src = imageBlockDataUri(block);
        return src === null ? [] : [src];
      }),
    ),
  ];
  return sources.map((src, index) => ({
    kind: "inline",
    src,
    alt: sources.length === 1 ? "Tool result image" : `Tool result image ${index + 1}`,
  }));
}

function outputBlocks(output: string): unknown[] {
  const trimmed = output.trim();
  const parsed = /^[[{]/.test(trimmed) ? parseJson(trimmed) : undefined;
  if (Array.isArray(parsed)) return parsed;
  // Omnigent's tagged MCP envelope and SDK Pi's results hold blocks in `content`.
  if (isRecord(parsed)) return Array.isArray(parsed.content) ? parsed.content : [parsed];
  // Several MCP blocks persist newline-joined: text lines and image-object lines.
  if (!trimmed.includes("\n")) return [];
  return trimmed
    .split("\n")
    .filter((line) => line.trimStart().startsWith("{"))
    .map((line) => parseJson(line));
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * A `data:` URI for an MCP (`data` + `mimeType`) or Anthropic (`source`)
 * image block, or `null` when the block holds no usable raster payload.
 */
function imageBlockDataUri(block: unknown): string | null {
  if (!isRecord(block) || block.type !== "image") return null;
  const source = isRecord(block.source) ? block.source : null;
  if (source !== null && source.type !== "base64") return null;
  const data = source === null ? block.data : source.data;
  const mediaType = source === null ? block.mimeType : source.media_type;
  if (typeof data !== "string" || typeof mediaType !== "string") return null;
  const normalizedType = mediaType.toLowerCase();
  if (!isRasterImageType(normalizedType)) return null;
  const compact = data.replace(/\s+/g, "");
  if (compact.length === 0 || !BASE64_RE.test(compact)) return null;
  return `data:${normalizedType};base64,${compact}`;
}

function baseName(path: string): string {
  return path.split("/").pop() || path;
}
