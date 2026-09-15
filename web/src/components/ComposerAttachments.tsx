import { useEffect, useState } from "react";
import { FileTextIcon, XIcon } from "lucide-react";

import { attachmentKey } from "@/lib/attachments";
import { ZoomableImage } from "@/components/ImageLightbox";
import { cn } from "@/lib/utils";

/**
 * Pending (pre-send) attachments shown under the composer textarea. Follows
 * Slack's pattern: images render as square thumbnails you can click to view
 * full-screen (via the shared lightbox), other files as a horizontal row with
 * name and type. Shared by the chat composer and the new-chat dialog.
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
    <div className={cn("flex flex-wrap items-start gap-1.5 px-4 pb-2", className)}>
      {files.map((file, i) => (
        <AttachmentTile key={attachmentKey(file)} file={file} onRemove={() => onRemove(i)} />
      ))}
    </div>
  );
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
  const isImage = file.type.startsWith("image/");
  // Pasted screenshots have no filename; the composer sends them as image.png.
  const name = file.name || "image.png";
  const url = useObjectUrl(isImage ? file : null);

  if (isImage) {
    return (
      <div className="relative size-16 shrink-0 overflow-hidden rounded-md border border-border bg-muted">
        <ZoomableImage src={url} alt={name} className="size-16 object-cover" />
        <RemoveButton
          name={name}
          onRemove={onRemove}
          className="absolute top-0.5 right-0.5 rounded-full bg-background/80 p-0.5 text-muted-foreground shadow-sm ring-1 ring-border hover:text-foreground"
        />
      </div>
    );
  }

  const ext = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1).toUpperCase() : "";
  return (
    <span className="flex items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1 text-sm text-muted-foreground">
      <FileTextIcon className="size-4 shrink-0" />
      <span className="max-w-[160px] truncate">{name}</span>
      {ext && <span className="shrink-0 text-xs opacity-70">{ext}</span>}
      <RemoveButton name={name} onRemove={onRemove} className="ml-0.5 rounded-full hover:text-foreground" />
    </span>
  );
}

function RemoveButton({
  name,
  onRemove,
  className,
}: {
  name: string;
  onRemove: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${name}`}
      className={cn("cursor-pointer", className)}
    >
      <XIcon className="size-3" />
    </button>
  );
}
