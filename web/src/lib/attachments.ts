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

/** Per-type upload size limits, in megabytes. Mirrors the server caps. */
export const ATTACHMENT_SIZE_LIMITS_MB = {
  image: 5,
  pdf: 20,
  text: 10,
} as const;

export type AttachmentCategory = keyof typeof ATTACHMENT_SIZE_LIMITS_MB;

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

function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot).toLowerCase() : "";
}

/**
 * Classify a file into an attachment category, or `null` if its type is not
 * supported (e.g. pptx, docx, xlsx, zip, binaries). Uses the browser MIME
 * type first, falling back to the filename extension for code/text files
 * whose MIME is unreliable.
 */
export function classifyAttachment(file: File): AttachmentCategory | null {
  const type = file.type || "";
  const ext = extensionOf(file.name || "");

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
        `"${name}" can't be attached — only images, PDF, and text/code files are supported.`,
      );
      continue;
    }
    const limitMb = ATTACHMENT_SIZE_LIMITS_MB[category];
    if (file.size > limitMb * 1024 * 1024) {
      errors.push(`"${name}" is too large — the limit for ${category} files is ${limitMb} MB.`);
      continue;
    }
    accepted.push(file);
  }

  return { accepted, errors };
}
