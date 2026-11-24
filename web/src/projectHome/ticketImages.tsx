import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type ReactNode,
} from "react";
import { PaperclipIcon } from "lucide-react";
import { ProseMarkdown } from "@/components/blocks/ChatMarkdown";
import { ComposerAttachments } from "@/components/ComposerAttachments";
import { SessionImage } from "@/components/SessionImage";
import { Button } from "@/components/ui/button";
import { clipboardFiles } from "@/lib/attachments";
import { authenticatedFetch } from "@/lib/identity";
import { readVideoResponse } from "@/lib/videoFetch";
import { uploadTicketImage } from "@/lib/projectTicketsApi";

const TICKET_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const TICKET_VIDEO_TYPES = new Set(["video/mp4", "video/webm", "video/quicktime"]);
// Marks an attachment link as a video, since the content path alone doesn't say.
const VIDEO_MARKER = "?type=video";
// Only images and videos uploaded to this server's ticket attachments render inline.
const TICKET_IMAGE_PATTERN =
  /!\[([^\]\n]*)\]\((\/v1\/projects\/[0-9a-f]{32}\/ticket-attachments\/[0-9a-f]{32}\/content)(\?type=video)?\)/g;

export type TicketBodySegment =
  { kind: "markdown"; text: string } | { kind: "image" | "video"; alt: string; path: string };

/** Split ticket markdown around uploaded-image references, keeping their order. */
export function splitTicketImages(body: string): TicketBodySegment[] {
  const segments: TicketBodySegment[] = [];
  let last = 0;
  for (const match of body.matchAll(TICKET_IMAGE_PATTERN)) {
    const before = body.slice(last, match.index);
    if (before.trim()) segments.push({ kind: "markdown", text: before });
    segments.push(
      match[3]
        ? { kind: "video", alt: match[1] || "Video", path: match[2] }
        : { kind: "image", alt: match[1] || "Image", path: match[2] },
    );
    last = match.index + match[0].length;
  }
  const rest = body.slice(last);
  if (rest.trim() || segments.length === 0) segments.push({ kind: "markdown", text: rest });
  return segments;
}

export function ticketImageMarkdown(filename: string, url: string, contentType?: string): string {
  const video = contentType !== undefined && TICKET_VIDEO_TYPES.has(contentType);
  const alt = filename.replace(/[[\]\n]/g, " ").trim() || (video ? "video" : "image");
  return `![${alt}](${url}${video ? VIDEO_MARKER : ""})`;
}

/** A ticket video, fetched with the viewer's credentials and played inline. */
export function TicketVideo({ path, alt }: { path: string; alt: string }) {
  const [source, setSource] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    setSource(null);
    setFailed(false);
    void (async () => {
      try {
        const response = await authenticatedFetch(path, { signal: controller.signal });
        if (!response.ok) throw new Error(`${response.status}`);
        const blob = await readVideoResponse(response, alt, { signal: controller.signal });
        objectUrl = URL.createObjectURL(blob);
        setSource(objectUrl);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, alt]);
  if (failed)
    return <p className="text-sm text-muted-foreground">The video {alt} can't be played.</p>;
  if (!source)
    return (
      <div
        role="status"
        aria-label={`Loading video ${alt}`}
        className="h-40 w-64 max-w-full animate-pulse rounded-lg bg-muted"
      />
    );
  return (
    <video
      src={source}
      controls
      preload="metadata"
      aria-label={alt}
      className="max-h-80 max-w-full rounded-lg border border-border bg-black"
    />
  );
}

/** Ticket markdown with uploaded images rendered through the authenticated image path. */
export function TicketMarkdown({ children, className }: { children: string; className?: string }) {
  const segments = splitTicketImages(children);
  if (segments.length === 1 && segments[0].kind === "markdown")
    return <ProseMarkdown className={className}>{children}</ProseMarkdown>;
  // Each segment's text or path plus its position keeps keys unique and stable.
  const seen = new Map<string, number>();
  const keyed = segments.map((segment) => {
    const base =
      segment.kind === "markdown" ? `text:${segment.text}` : `${segment.kind}:${segment.path}`;
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return { segment, key: `${base}#${repeat}` };
  });
  return (
    <div className="space-y-2">
      {keyed.map(({ segment, key }): ReactNode =>
        segment.kind === "markdown" ? (
          <ProseMarkdown key={key} className={className}>
            {segment.text}
          </ProseMarkdown>
        ) : segment.kind === "video" ? (
          <TicketVideo key={key} path={segment.path} alt={segment.alt} />
        ) : (
          <SessionImage key={key} path={segment.path} alt={segment.alt} />
        ),
      )}
    </div>
  );
}

function imageFiles(files: File[]): File[] {
  return files.filter(
    (file) => TICKET_IMAGE_TYPES.has(file.type) || TICKET_VIDEO_TYPES.has(file.type),
  );
}

/** An image picked for a ticket or comment, shown as a thumbnail until it's sent. */
export interface PendingTicketImage {
  id: number;
  file: File;
  /** The image's markdown link once uploaded, e.g. ``![shot.png](/v1/.../content)``. */
  markdown: string | null;
}

/**
 * Uploads pasted, dropped or picked images and keeps them as thumbnails beside the
 * text. {@link withImages} appends their markdown when the text is sent, so stored
 * tickets and comments keep the same format. `resolveProjectId` may create the
 * project first, as a new ticket does.
 */
export function useTicketImageUpload(resolveProjectId: () => Promise<string>) {
  const [images, setImages] = useState<PendingTicketImage[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const resolveRef = useRef(resolveProjectId);
  resolveRef.current = resolveProjectId;
  const nextId = useRef(0);

  const upload = (files: File[]) => {
    const added: PendingTicketImage[] = files.map((file) => ({
      id: nextId.current++,
      file,
      markdown: null,
    }));
    setError(null);
    setImages((previous) => [...previous, ...added]);
    void (async () => {
      let projectId: string;
      try {
        projectId = await resolveRef.current();
      } catch (caught) {
        setError(caught instanceof Error ? caught : new Error(String(caught)));
        setImages((previous) => previous.filter((image) => !added.includes(image)));
        return;
      }
      // One at a time, in pick order; a failed image doesn't stop the rest.
      await added.reduce(async (previousUpload, pending) => {
        await previousUpload;
        try {
          const uploaded = await uploadTicketImage(projectId, pending.file);
          const markdown = ticketImageMarkdown(
            uploaded.filename,
            uploaded.url,
            uploaded.content_type ?? pending.file.type,
          );
          setImages((previous) =>
            previous.map((image) => (image.id === pending.id ? { ...image, markdown } : image)),
          );
        } catch (caught) {
          setError(caught instanceof Error ? caught : new Error(String(caught)));
          setImages((previous) => previous.filter((image) => image.id !== pending.id));
        }
      }, Promise.resolve());
    })();
  };

  return {
    images,
    uploading: images.some((image) => image.markdown === null),
    error,
    remove: (index: number) => setImages((previous) => previous.filter((_, at) => at !== index)),
    clear: () => setImages([]),
    /** *text* with the uploaded images' markdown appended, one per line. */
    withImages: (text: string) => {
      const links = images.flatMap((image) => (image.markdown ? [image.markdown] : []));
      if (!links.length) return text;
      return text.trim() ? `${text.trimEnd()}\n\n${links.join("\n")}` : links.join("\n");
    },
    attach: (files: File[]) => {
      const accepted = imageFiles(files);
      if (accepted.length) upload(accepted);
    },
    onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => {
      const files = imageFiles(clipboardFiles(event.clipboardData));
      if (!files.length) return;
      event.preventDefault();
      upload(files);
    },
    onDragOver: (event: DragEvent<HTMLTextAreaElement>) => {
      if ([...event.dataTransfer.items].some((item) => item.kind === "file"))
        event.preventDefault();
    },
    onDrop: (event: DragEvent<HTMLTextAreaElement>) => {
      const files = imageFiles([...event.dataTransfer.files]);
      if (!files.length) return;
      event.preventDefault();
      upload(files);
    },
  };
}

/** Thumbnails of a {@link useTicketImageUpload}'s images, as the chat composer shows them. */
export function TicketImagePreviews({
  upload,
  className,
}: {
  upload: Pick<ReturnType<typeof useTicketImageUpload>, "images" | "remove">;
  className?: string;
}) {
  return (
    <ComposerAttachments
      files={upload.images.map((image) => image.file)}
      onRemove={upload.remove}
      className={className}
    />
  );
}

/** Picks images from disk for {@link useTicketImageUpload}, beside paste and drop. */
export function TicketImageAttachButton({
  onFiles,
  disabled,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={input}
        type="file"
        accept={[...TICKET_IMAGE_TYPES, ...TICKET_VIDEO_TYPES].join(",")}
        multiple
        hidden
        aria-hidden
        tabIndex={-1}
        onChange={(event) => {
          onFiles([...(event.target.files ?? [])]);
          event.target.value = "";
        }}
      />
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="text-muted-foreground"
        disabled={disabled}
        title="Attach images or videos. You can also paste or drop them into the text box."
        onClick={() => input.current?.click()}
      >
        <PaperclipIcon aria-hidden />
        Attach media
      </Button>
    </>
  );
}
