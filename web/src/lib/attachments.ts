import type { FilesystemAttachmentPolicy } from "./capabilities";
import { formatBytes } from "@/shell/fileStatusUtils";

// Inline limits mirror content_resolver.py; filesystem limits come from /v1/info.
export const ATTACHMENT_SIZE_LIMITS_MB = {
  image: 50,
  pdf: 20,
  text: 10,
} as const;

// Raster image types the server can compress under the model limit; only these
// get the large image cap. Mirrors _COMPRESSIBLE_IMAGE_MIMES on the server.
const COMPRESSIBLE_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);

// Image types we don't compress (SVG, …) keep this smaller cap. Mirrors
// IMAGE_UNCOMPRESSED_UPLOAD_BYTES on the server.
const UNCOMPRESSED_IMAGE_LIMIT_MB = 5;

export type AttachmentCategory = keyof typeof ATTACHMENT_SIZE_LIMITS_MB | "file";

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

function normalizedFilename(filename: string): string {
  return filename
    .replace(/\p{Cf}/gu, "")
    .toLowerCase()
    .replace(/[.\p{White_Space}]+$/gu, "");
}

function extensionOf(filename: string): string {
  const name = normalizedFilename(filename);
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot) : "";
}

function deniedFilename(filename: string, denied: string[]): boolean {
  const name = normalizedFilename(filename)
    .split(":")
    .map((part) =>
      part
        .split(".")
        .map((segment) => segment.trimEnd())
        .join("."),
    )
    .join(":");
  return denied.some((extension) => {
    let index = name.indexOf(extension);
    while (index !== -1) {
      const end = index + extension.length;
      if (end === name.length || name[end] === "." || name[end] === ":") return true;
      index = name.indexOf(extension, index + 1);
    }
    return false;
  });
}

// Legacy filesystem formats cannot become inline through a browser MIME hint.
const LEGACY_FILESYSTEM_SUFFIXES = [
  ".zip",
  ".docx",
  ".xlsx",
  ".pptx",
  ".db",
  ".sqlite",
  ".sqlite3",
];
const TEXT_APPLICATION_MIMES = new Set([
  "application/json",
  "application/javascript",
  "application/jsonl",
  "application/x-ndjson",
  "application/x-ipynb+json",
]);

/** Explicit filesystem extensions take precedence over browser MIME hints. */
export function classifyAttachment(
  file: File,
  policy?: FilesystemAttachmentPolicy,
): AttachmentCategory | null {
  const name = normalizedFilename(attachmentFilename(file));
  const ext = extensionOf(name);
  const matches = (suffix: string) =>
    name.endsWith(suffix) || normalizedFilename(name.split(":")[0]).endsWith(suffix);
  if (Array.isArray(policy?.allowed_extensions) && policy.allowed_extensions.some(matches))
    return "file";
  if (LEGACY_FILESYSTEM_SUFFIXES.some(matches)) {
    return !policy || policy.allowed_extensions === "*" ? "file" : null;
  }
  // Under "*", MIME hints steer only extensionless or generic names such as payload.bin.
  if (policy?.allowed_extensions === "*" && policy.non_inline_extensions?.some(matches))
    return "file";
  const mime = file.type.split(";", 1)[0].trim().toLowerCase();
  const publishedInline = policy?.inline_extensions?.[ext];
  if (mime && mime !== "application/octet-stream") {
    if (mime.startsWith("image/")) return "image";
    if (mime === "application/pdf") return "pdf";
    if (mime.startsWith("text/") || TEXT_APPLICATION_MIMES.has(mime)) return "text";
  } else {
    if (publishedInline) return publishedInline;
    if (/\.(png|jpe?g|gif|webp|svg|bmp|tiff?|ico|avif|heic|heif)$/.test(ext)) return "image";
    if (ext === ".pdf") return "pdf";
  }
  if (publishedInline === "text" || TEXT_CODE_EXTENSIONS.has(ext)) return "text";
  return !policy || policy.allowed_extensions === "*" ? "file" : null;
}

/** Leave the OS picker unrestricted when the server owns unknown-type admission. */
export function attachmentAccept(policy?: FilesystemAttachmentPolicy): string | undefined {
  if (!policy || policy.allowed_extensions === "*") return undefined;
  return [
    "image/*",
    "application/pdf",
    "text/*",
    "application/json",
    ...TEXT_CODE_EXTENSIONS,
    ...Object.keys(policy.inline_extensions ?? {}),
    ...policy.allowed_extensions,
  ]
    .filter((entry) => !deniedFilename(`file${entry}`, policy.denied_extensions))
    .join(",");
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
export function validateAttachments(
  files: File[],
  policy?: FilesystemAttachmentPolicy,
  existing: File[] = [],
): AttachmentValidation {
  const accepted: File[] = [];
  const errors: string[] = [];

  const filesystemFiles = existing.filter((file) => classifyAttachment(file, policy) === "file");
  let filesystemCount = filesystemFiles.length;
  let filesystemBytes = filesystemFiles.reduce((sum, file) => sum + file.size, 0);
  for (const file of files) {
    const name = file.name || "file";
    const filename = attachmentFilename(file);
    if (filename.length > 255 || new TextEncoder().encode(filename).length > 255) {
      errors.push(`"${name}" has an invalid filename.`);
      continue;
    }
    const decodedFilename = filename.replace(/(?:%[a-f0-9]{2})+/gi, (encoded) =>
      new TextDecoder().decode(
        Uint8Array.from(encoded.match(/[a-f0-9]{2}/gi) ?? [], (hex) => parseInt(hex, 16)),
      ),
    );
    if (
      /[\p{Cc}\p{Zl}\p{Zp}\u202a-\u202e\u2066-\u2069]/u.test(decodedFilename) ||
      /[/\\]/.test(filename) ||
      !normalizedFilename(filename)
    ) {
      errors.push(`"${name}" has an invalid filename.`);
      continue;
    }
    const category = classifyAttachment(file, policy);
    if (category === "file" && decodedFilename.includes(":")) {
      errors.push(`"${name}" has an invalid filesystem filename.`);
      continue;
    }
    if (policy && deniedFilename(filename, policy.denied_extensions)) {
      errors.push(`"${name}" can't be attached: this extension is denied by server policy.`);
      continue;
    }
    if (category === null) {
      errors.push(`"${name}" can't be attached: this file type is not allowed by server policy.`);
      continue;
    }
    if (category === "file") {
      if (policy && file.size > policy.max_bytes) {
        errors.push(
          `"${name}" is too large — the server limit is ${formatBytes(policy.max_bytes)}.`,
        );
      } else if (policy && filesystemCount >= policy.max_files) {
        errors.push(`"${name}" exceeds the server limit of ${policy.max_files} files.`);
      } else if (policy && filesystemBytes + file.size > policy.max_total_bytes) {
        errors.push(`"${name}" exceeds the server total attachment size limit.`);
      } else {
        accepted.push(file);
        filesystemCount++;
        filesystemBytes += file.size;
      }
      continue;
    }
    // Non-compressible images (SVG, …) can't be shrunk server-side, so they
    // keep the smaller cap; compressible raster images get the large cap.
    const limitMb =
      category === "image" && !COMPRESSIBLE_IMAGE_MIMES.has(file.type || "")
        ? UNCOMPRESSED_IMAGE_LIMIT_MB
        : ATTACHMENT_SIZE_LIMITS_MB[category];
    if (file.size > limitMb * 1024 * 1024) {
      const limitLabel = `${category} files`;
      errors.push(`"${name}" is too large — the limit for ${limitLabel} is ${limitMb} MB.`);
      continue;
    }
    accepted.push(file);
  }

  return { accepted, errors };
}
