import { useRef, useState, type ClipboardEvent, type DragEvent, type ReactNode } from "react";
import { ProseMarkdown } from "@/components/blocks/ChatMarkdown";
import { SessionImage } from "@/components/SessionImage";
import { clipboardFiles } from "@/lib/attachments";
import { uploadTicketImage } from "@/lib/projectTicketsApi";

const TICKET_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
// Only images uploaded to this server's ticket attachments render inline.
const TICKET_IMAGE_PATTERN =
  /!\[([^\]\n]*)\]\((\/v1\/projects\/[0-9a-f]{32}\/ticket-attachments\/[0-9a-f]{32}\/content)\)/g;

export type TicketBodySegment =
  | { kind: "markdown"; text: string }
  | { kind: "image"; alt: string; path: string };

/** Split ticket markdown around uploaded-image references, keeping their order. */
export function splitTicketImages(body: string): TicketBodySegment[] {
  const segments: TicketBodySegment[] = [];
  let last = 0;
  for (const match of body.matchAll(TICKET_IMAGE_PATTERN)) {
    const before = body.slice(last, match.index);
    if (before.trim()) segments.push({ kind: "markdown", text: before });
    segments.push({ kind: "image", alt: match[1] || "Image", path: match[2] });
    last = match.index + match[0].length;
  }
  const rest = body.slice(last);
  if (rest.trim() || segments.length === 0) segments.push({ kind: "markdown", text: rest });
  return segments;
}

export function ticketImageMarkdown(filename: string, url: string): string {
  const alt = filename.replace(/[[\]\n]/g, " ").trim() || "image";
  return `![${alt}](${url})`;
}

/** Ticket markdown with uploaded images rendered through the authenticated image path. */
export function TicketMarkdown({ children, className }: { children: string; className?: string }) {
  const segments = splitTicketImages(children);
  if (segments.length === 1 && segments[0].kind === "markdown")
    return <ProseMarkdown className={className}>{children}</ProseMarkdown>;
  return (
    <div className="space-y-2">
      {segments.map(
        (segment, index): ReactNode =>
          segment.kind === "markdown" ? (
            <ProseMarkdown key={index} className={className}>
              {segment.text}
            </ProseMarkdown>
          ) : (
            <SessionImage key={index} path={segment.path} alt={segment.alt} />
          ),
      )}
    </div>
  );
}

function imageFiles(files: File[]): File[] {
  return files.filter((file) => TICKET_IMAGE_TYPES.has(file.type));
}

/**
 * Paste/drop handlers that upload images and insert their markdown at the caret.
 * `resolveProjectId` may create the project first, as a new ticket does.
 */
export function useTicketImageUpload(
  resolveProjectId: () => Promise<string>,
  setValue: (update: (previous: string) => string) => void,
) {
  const [uploading, setUploading] = useState(0);
  const [error, setError] = useState<Error | null>(null);
  const resolveRef = useRef(resolveProjectId);
  resolveRef.current = resolveProjectId;

  const upload = (files: File[], target: HTMLTextAreaElement) => {
    const caret = target.selectionEnd ?? target.value.length;
    setError(null);
    setUploading((count) => count + files.length);
    void (async () => {
      try {
        const projectId = await resolveRef.current();
        for (const file of files) {
          try {
            const image = await uploadTicketImage(projectId, file);
            const markdown = ticketImageMarkdown(image.filename, image.url);
            setValue((previous) => {
              const at = Math.min(caret, previous.length);
              const before = previous.slice(0, at);
              const after = previous.slice(at);
              const lead = before && !before.endsWith("\n") ? "\n" : "";
              const trail = after.startsWith("\n") ? "" : "\n";
              return `${before}${lead}${markdown}${trail}${after}`;
            });
          } finally {
            setUploading((count) => count - 1);
          }
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught : new Error(String(caught)));
        setUploading(0);
      }
    })();
  };

  return {
    uploading: uploading > 0,
    error,
    onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => {
      const files = imageFiles(clipboardFiles(event.clipboardData));
      if (!files.length) return;
      event.preventDefault();
      upload(files, event.currentTarget);
    },
    onDragOver: (event: DragEvent<HTMLTextAreaElement>) => {
      if ([...event.dataTransfer.items].some((item) => item.kind === "file"))
        event.preventDefault();
    },
    onDrop: (event: DragEvent<HTMLTextAreaElement>) => {
      const files = imageFiles([...event.dataTransfer.files]);
      if (!files.length) return;
      event.preventDefault();
      upload(files, event.currentTarget);
    },
  };
}
