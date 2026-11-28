import type { LucideIcon } from "lucide-react";
import {
  DatabaseIcon,
  FileArchiveIcon,
  FileAudioIcon,
  FileCode2Icon,
  FileIcon,
  FileImageIcon,
  FileJsonIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  FileTypeIcon,
  FileVideoIcon,
  PresentationIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

type FileTypeGroup =
  | "pdf"
  | "document"
  | "spreadsheet"
  | "presentation"
  | "archive"
  | "json"
  | "code"
  | "image"
  | "video"
  | "audio"
  | "database"
  | "font"
  | "generic";

type TagColor =
  "coral" | "blue" | "lime" | "lemon" | "brown" | "purple" | "pink" | "indigo" | "default";

interface FileVisual {
  group: FileTypeGroup;
  tagColor: TagColor;
  Icon: LucideIcon;
  colorClass: string;
}

const DOCUMENT_EXTENSIONS = new Set(["doc", "docx", "txt", "md", "rtf"]);
const SPREADSHEET_EXTENSIONS = new Set(["xls", "xlsx", "csv", "tsv"]);
const PRESENTATION_EXTENSIONS = new Set(["ppt", "pptx", "key"]);
const ARCHIVE_EXTENSIONS = new Set(["zip", "rar", "7z", "tar", "gz"]);
const JSON_EXTENSIONS = new Set(["json", "jsonl"]);
const CODE_EXTENSIONS = new Set([
  "js",
  "jsx",
  "ts",
  "tsx",
  "py",
  "rb",
  "go",
  "rs",
  "java",
  "html",
  "css",
  "xml",
  "yaml",
  "yml",
  "toml",
]);
const IMAGE_EXTENSIONS = new Set([
  "avif",
  "bmp",
  "gif",
  "heic",
  "heif",
  "ico",
  "jpeg",
  "jpg",
  "png",
  "svg",
  "tif",
  "tiff",
  "webp",
]);
const VIDEO_EXTENSIONS = new Set([
  "avi",
  "flv",
  "m4v",
  "mkv",
  "mov",
  "mp4",
  "mpeg",
  "mpg",
  "webm",
  "wmv",
]);
const AUDIO_EXTENSIONS = new Set(["aac", "flac", "m4a", "mp3", "ogg", "opus", "wav", "wma"]);
const DATABASE_EXTENSIONS = new Set(["sql", "db", "sqlite", "sqlite3"]);
const FONT_EXTENSIONS = new Set(["ttf", "otf", "woff", "woff2"]);

const VISUALS: Record<FileTypeGroup, FileVisual> = {
  pdf: {
    group: "pdf",
    tagColor: "coral",
    Icon: FileTextIcon,
    colorClass: "text-orange-500 dark:text-orange-400",
  },
  document: {
    group: "document",
    tagColor: "blue",
    Icon: FileTextIcon,
    colorClass: "text-blue-500 dark:text-blue-400",
  },
  spreadsheet: {
    group: "spreadsheet",
    tagColor: "lime",
    Icon: FileSpreadsheetIcon,
    colorClass: "text-lime-600 dark:text-lime-400",
  },
  presentation: {
    group: "presentation",
    tagColor: "lemon",
    Icon: PresentationIcon,
    colorClass: "text-yellow-500 dark:text-yellow-400",
  },
  archive: {
    group: "archive",
    tagColor: "brown",
    Icon: FileArchiveIcon,
    colorClass: "text-amber-700 dark:text-amber-500",
  },
  json: {
    group: "json",
    tagColor: "purple",
    Icon: FileJsonIcon,
    colorClass: "text-purple-500 dark:text-purple-400",
  },
  code: {
    group: "code",
    tagColor: "purple",
    Icon: FileCode2Icon,
    colorClass: "text-purple-500 dark:text-purple-400",
  },
  image: {
    group: "image",
    tagColor: "pink",
    Icon: FileImageIcon,
    colorClass: "text-pink-500 dark:text-pink-400",
  },
  video: {
    group: "video",
    tagColor: "indigo",
    Icon: FileVideoIcon,
    colorClass: "text-indigo-500 dark:text-indigo-400",
  },
  audio: {
    group: "audio",
    tagColor: "indigo",
    Icon: FileAudioIcon,
    colorClass: "text-indigo-500 dark:text-indigo-400",
  },
  database: {
    group: "database",
    tagColor: "purple",
    Icon: DatabaseIcon,
    colorClass: "text-purple-500 dark:text-purple-400",
  },
  font: {
    group: "font",
    tagColor: "default",
    Icon: FileTypeIcon,
    colorClass: "text-muted-foreground",
  },
  generic: {
    group: "generic",
    tagColor: "default",
    Icon: FileIcon,
    colorClass: "text-muted-foreground",
  },
};

function extensionOf(path: string): string {
  const name = path.split("/").at(-1) ?? path;
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function workspaceFileVisual(path: string, mimeType?: string | null): FileVisual {
  const mime = mimeType?.split(";")[0]?.trim().toLowerCase() ?? "";
  if (mime === "application/pdf") return VISUALS.pdf;
  if (mime.startsWith("image/")) return VISUALS.image;
  if (mime.startsWith("video/")) return VISUALS.video;
  if (mime.startsWith("audio/")) return VISUALS.audio;
  if (mime === "application/json" || mime.endsWith("+json")) return VISUALS.json;

  const extension = extensionOf(path);
  if (extension === "pdf") return VISUALS.pdf;
  if (DOCUMENT_EXTENSIONS.has(extension)) return VISUALS.document;
  if (SPREADSHEET_EXTENSIONS.has(extension)) return VISUALS.spreadsheet;
  if (PRESENTATION_EXTENSIONS.has(extension)) return VISUALS.presentation;
  if (ARCHIVE_EXTENSIONS.has(extension)) return VISUALS.archive;
  if (JSON_EXTENSIONS.has(extension)) return VISUALS.json;
  if (CODE_EXTENSIONS.has(extension)) return VISUALS.code;
  if (IMAGE_EXTENSIONS.has(extension)) return VISUALS.image;
  if (VIDEO_EXTENSIONS.has(extension)) return VISUALS.video;
  if (AUDIO_EXTENSIONS.has(extension)) return VISUALS.audio;
  if (DATABASE_EXTENSIONS.has(extension)) return VISUALS.database;
  if (FONT_EXTENSIONS.has(extension)) return VISUALS.font;
  return VISUALS.generic;
}

export function WorkspaceFileIcon({
  path,
  mimeType,
  className,
}: {
  path: string;
  mimeType?: string | null;
  className?: string;
}) {
  const visual = workspaceFileVisual(path, mimeType);
  return (
    <visual.Icon
      aria-hidden
      data-file-type={visual.group}
      data-tag-color={visual.tagColor}
      className={cn("size-3.5 shrink-0", visual.colorClass, className)}
    />
  );
}
