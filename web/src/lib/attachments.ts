/**
 * Client-side attachment validation: which files can be attached, and how
 * large each type may be.
 *
 * This mirrors the authoritative server-side checks in
 * omnigent/runtime/content_resolver.py (`attachment_upload_limit`) and the
 * upload route (415 for unsupported types, 413 for oversized). Keeping a
 * copy here lets us reject a bad file at paste/drop/pick time — before a
 * slow upload — with a friendly message. The server still enforces; this is
 * UX only. Keep the limits in sync with the Python constants.
 */

/**
 * Per-type upload size limits, in megabytes. Mirrors the server caps.
 *
 * Compressible raster images accept a large upload — the server
 * downscales/re-encodes an oversized one under the provider's per-image limit
 * before storing it, so screenshots and retina captures no longer need to be
 * shrunk by hand. Other image types (SVG, …) can't be shrunk, so they keep the
 * smaller `UNCOMPRESSED_IMAGE_LIMIT_MB` cap (see `validateAttachments`).
 *
 * These are fixed client-side ceilings, so a deployment that raises a server
 * limit (e.g. `filesystem_attachment_max_bytes`) also needs these raised for the
 * extra allowance to be usable from the web UI.
 */
export const ATTACHMENT_SIZE_LIMITS_MB = {
  image: 50,
  pdf: 20,
  text: 10,
  file: 50,
} as const;

// Raster image types the server can compress under the model limit; only these
// get the large image cap. Mirrors _COMPRESSIBLE_IMAGE_MIMES on the server.
const COMPRESSIBLE_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

// Image types we don't compress (SVG, …) keep this smaller cap. Mirrors
// IMAGE_UNCOMPRESSED_UPLOAD_BYTES on the server.
const UNCOMPRESSED_IMAGE_LIMIT_MB = 5;

export type AttachmentCategory = keyof typeof ATTACHMENT_SIZE_LIMITS_MB;

/** Keep unnamed clipboard images consistent before and after upload. */
export function attachmentFilename(file: File): string {
  return file.name || "image.png";
}

const attachmentIds = new WeakMap<File, string>();
let nextAttachmentId = 0;

export function attachmentKey(file: File): string {
  const existing = attachmentIds.get(file);
  if (existing) return existing;
  const id = `attachment-${nextAttachmentId++}`;
  attachmentIds.set(file, id);
  return id;
}

// Text-bearing application/* MIME types (the rest of the text-like surface
// is text/*). Mirrors _TEXT_LIKE_APPLICATION_MIMES on the server.
const TEXT_LIKE_APPLICATION_MIMES = new Set([
  "application/json",
  "application/javascript",
  "application/jsonl",
  "application/x-ndjson",
  "application/x-ipynb+json",
]);

// Text/code extensions whose browser-reported MIME type is often empty or
// wrong (e.g. a .ts file reports video/mp2t, .rs reports nothing). Mirrors
// the code entries in _EXTRA_MIME_TYPES on the server so we accept the same
// files the backend resolves to a text/* type.
const TEXT_CODE_EXTENSIONS = new Set([
  ".txt",
  ".log",
  ".md",
  ".markdown",
  ".csv",
  ".json",
  ".jsonl",
  ".ndjson",
  ".yaml",
  ".yml",
  ".toml",
  ".ini",
  ".cfg",
  ".env",
  ".lock",
  ".proto",
  ".graphql",
  ".gql",
  ".html",
  ".htm",
  ".xml",
  ".css",
  ".js",
  ".jsx",
  ".mjs",
  ".cjs",
  ".ts",
  ".tsx",
  ".py",
  ".rb",
  ".go",
  ".rs",
  ".java",
  ".kt",
  ".scala",
  ".swift",
  ".c",
  ".h",
  ".cc",
  ".cpp",
  ".hpp",
  ".cs",
  ".php",
  ".pl",
  ".r",
  ".jl",
  ".lua",
  ".ex",
  ".exs",
  ".erl",
  ".hs",
  ".clj",
  ".dart",
  ".vue",
  ".svelte",
  ".sh",
  ".bash",
  ".zsh",
  ".fish",
  ".sql",
  ".tf",
  ".hcl",
  ".gradle",
  ".dockerfile",
  ".ipynb",
]);

// Archives, Office documents, and databases require filesystem tools.
// Mirrors _FILESYSTEM_ATTACHMENT_EXTENSIONS in omnigent/inner/native_attachments.py.
const FILESYSTEM_ATTACHMENT_EXTENSIONS = new Set([
  ".zip",
  ".docx",
  ".xlsx",
  ".pptx",
  ".db",
  ".sqlite",
  ".sqlite3",
]);

function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot).toLowerCase() : "";
}

/**
 * Classify a file into an attachment category, or `null` if its type is not
 * supported (e.g. audio, video, unrecognised binaries). Files requiring local
 * tools are identified by extension; other types also use the browser MIME.
 */
export function classifyAttachment(file: File): AttachmentCategory | null {
  const type = file.type || "";
  const ext = extensionOf(file.name || "");

  // Match the server even when a browser mislabels a ZIP as text/plain.
  if (FILESYSTEM_ATTACHMENT_EXTENSIONS.has(ext)) return "file";
  if (type.startsWith("image/")) return "image";
  if (type === "application/pdf" || ext === ".pdf") return "pdf";
  if (
    type.startsWith("text/") ||
    TEXT_LIKE_APPLICATION_MIMES.has(type) ||
    TEXT_CODE_EXTENSIONS.has(ext)
  ) {
    return "text";
  }
  return null;
}

export interface AttachmentValidation {
  /** Files that passed type + size checks. */
  accepted: File[];
  /** Human-readable rejection messages, one per rejected file. */
  errors: string[];
}

/**
 * Split *files* into accepted attachments and rejection messages. A file is
 * rejected when its type is unsupported, or when it exceeds the per-type
 * size limit.
 */
export function validateAttachments(files: File[]): AttachmentValidation {
  const accepted: File[] = [];
  const errors: string[] = [];

  for (const file of files) {
    const name = file.name || "file";
    const category = classifyAttachment(file);
    if (category === null) {
      errors.push(
        `"${name}" can't be attached: only images, PDF, text/code, archives, ` +
          `office documents, and databases are supported.`,
      );
      continue;
    }
    // Non-compressible images (SVG, …) can't be shrunk server-side, so they
    // keep the smaller cap; compressible raster images get the large cap.
    const limitMb =
      category === "image" && !COMPRESSIBLE_IMAGE_MIMES.has(file.type || "")
        ? UNCOMPRESSED_IMAGE_LIMIT_MB
        : ATTACHMENT_SIZE_LIMITS_MB[category];
    if (file.size > limitMb * 1024 * 1024) {
      const limitLabel = category === "file" ? "files" : `${category} files`;
      errors.push(`"${name}" is too large — the limit for ${limitLabel} is ${limitMb} MB.`);
      continue;
    }
    accepted.push(file);
  }

  return { accepted, errors };
}
