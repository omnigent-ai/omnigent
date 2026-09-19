import { useEffect, useState } from "react";
import {
  Database,
  File as FileIcon,
  FileArchive,
  FileAudio,
  FileCode2,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType,
  FileVideo,
  Presentation,
  XIcon,
  type LucideIcon,
} from "lucide-react";

import { attachmentFilename, attachmentKey, classifyAttachment } from "@/lib/attachments";
import { ZoomableImage } from "@/components/ImageLightbox";
import { cn } from "@/lib/utils";

/**
 * Pending (pre-send) attachments shown under the composer textarea. A supported
 * image renders as a square thumbnail you can click to view full-screen (via
 * the shared lightbox); anything else — and an image whose thumbnail fails to
 * load — renders as a card with a file-type icon, the filename, and a
 * "TYPE · SIZE" line. Shared by the chat composer and the new-chat dialog.
 */
export function ComposerAttachments({
  files,
  onRemove,
  className,
}: {
  files: File[];
  onRemove: (index: number) => void;
  className?: string;
}) {
  if (files.length === 0) return null;
  return (
    <div className={cn("flex flex-wrap items-start gap-2 px-4 pb-2", className)}>
      {files.map((file, i) => (
        <AttachmentTile key={attachmentKey(file)} file={file} onRemove={() => onRemove(i)} />
      ))}
    </div>
  );
}

/** Human-readable file size, e.g. 6815744 -> "6.5 MB". */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

// Lowercase extension -> Lucide icon. Grouped to match the design spec.
const EXTENSION_ICONS: Record<string, LucideIcon> = {
  pdf: FileText,
  doc: FileText,
  docx: FileText,
  txt: FileText,
  md: FileText,
  rtf: FileText,
  xls: FileSpreadsheet,
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
  tsv: FileSpreadsheet,
  ppt: Presentation,
  pptx: Presentation,
  key: Presentation,
  zip: FileArchive,
  rar: FileArchive,
  "7z": FileArchive,
  tar: FileArchive,
  gz: FileArchive,
  json: FileJson,
  jsonl: FileJson,
  js: FileCode2,
  jsx: FileCode2,
  ts: FileCode2,
  tsx: FileCode2,
  py: FileCode2,
  rb: FileCode2,
  go: FileCode2,
  rs: FileCode2,
  java: FileCode2,
  html: FileCode2,
  css: FileCode2,
  xml: FileCode2,
  yaml: FileCode2,
  yml: FileCode2,
  toml: FileCode2,
  sql: Database,
  db: Database,
  sqlite: Database,
  sqlite3: Database,
  ttf: FileType,
  otf: FileType,
  woff: FileType,
  woff2: FileType,
};

/** Icon for a file: match by extension first, then MIME prefix, else generic. */
export function iconForFile(file: File): LucideIcon {
  const name = file.name || "";
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (ext && EXTENSION_ICONS[ext]) return EXTENSION_ICONS[ext];
  const type = file.type || "";
  if (type.startsWith("image/")) return FileImage;
  if (type.startsWith("video/")) return FileVideo;
  if (type.startsWith("audio/")) return FileAudio;
  return FileIcon;
}

/** Blob URL for an image preview, created and revoked inside one effect so the
 *  URL the committed <img> points at is never revoked early (StrictMode double
 *  mount) and never leaks. Guarded: jsdom (tests) lacks createObjectURL. */
function useObjectUrl(file: File | null): string | undefined {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (!file || typeof URL.createObjectURL !== "function") {
      setUrl(undefined);
      return;
    }
    const objectUrl = URL.createObjectURL(file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [file]);
  return url;
}

function AttachmentTile({ file, onRemove }: { file: File; onRemove: () => void }) {
  // An image whose blob can't decode falls back to the file card (spec rule 4).
  const [thumbFailed, setThumbFailed] = useState(false);
  const showThumb = file.type.startsWith("image/") && !thumbFailed;
  const name = attachmentFilename(file);
  const url = useObjectUrl(showThumb ? file : null);

  if (showThumb) {
    return (
      <div className="relative shrink-0">
        <div className="size-14 overflow-hidden rounded-xl border border-border bg-muted">
          <ZoomableImage
            src={url}
            alt={name}
            className="size-14 object-cover"
            onError={() => setThumbFailed(true)}
          />
        </div>
        <RemoveButton name={name} onRemove={onRemove} />
      </div>
    );
  }

  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toUpperCase() : "";
  const meta = ext ? `${ext} · ${formatFileSize(file.size)}` : formatFileSize(file.size);
  const Icon = iconForFile(file);
  // Archives, office documents and databases go to the session workspace for
  // the agent to open rather than into the model context; say so on the card.
  const toWorkspace = classifyAttachment(file) === "workspace";
  return (
    <div className="relative shrink-0">
      <div
        className="flex h-14 w-[180px] items-center gap-2 rounded-xl border border-border bg-background p-2"
        title={
          toWorkspace
            ? `${name} will be placed in the session workspace for the agent to open`
            : undefined
        }
      >
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
          <Icon className="size-5" />
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium text-foreground">{name}</span>
          <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
            <span className="truncate">{meta}</span>
            {toWorkspace && (
              <span className="shrink-0 rounded-sm bg-muted px-1 text-[10px] leading-4">
                workspace
              </span>
            )}
          </span>
        </span>
      </div>
      <RemoveButton name={name} onRemove={onRemove} />
    </div>
  );
}

/** The 24px circular remove control, overlapping the tile's top-right corner. */
function RemoveButton({ name, onRemove }: { name: string; onRemove: () => void }) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${name}`}
      className="absolute -top-1 -right-1 grid size-6 cursor-pointer place-items-center rounded-full border border-border bg-background text-muted-foreground shadow-sm hover:text-foreground"
    >
      <XIcon className="size-3.5" />
    </button>
  );
}
