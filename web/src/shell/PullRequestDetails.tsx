// The PR's title (in the panel header) and description (in the Summary tab), each
// editable in place by editors. The description editor is a raw markdown textarea
// with a preview; pasted, dropped, or picked images and videos upload at once and
// are embedded as provider URLs, so saving is one plain text edit.

import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { ExternalLinkIcon, Loader2Icon, PaperclipIcon, PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { Components } from "streamdown";
import { MessageResponse } from "@/components/ai-elements/message";
import { cn } from "@/lib/utils";
import {
  uploadPullRequestAttachment,
  useUpdatePullRequestDetails,
  type PullRequestAttachment,
} from "@/hooks/usePullRequests";

// The media types the provider accepts as description attachments.
const ATTACHMENT_ACCEPT =
  "image/png,image/jpeg,image/gif,image/webp,video/mp4,video/quicktime,video/webm";

function isAttachableMedia(file: File): boolean {
  return file.type.startsWith("image/") || file.type.startsWith("video/");
}

// The server's per-type limits, checked here so a too-large file isn't sent first.
const MAX_IMAGE_ATTACHMENT_MB = 10;
const MAX_VIDEO_ATTACHMENT_MB = 70;

/** Why `file` is too large to attach, or `null` when it fits. */
function attachmentSizeError(file: File): string | null {
  const video = file.type.startsWith("video/");
  const limitMb = video ? MAX_VIDEO_ATTACHMENT_MB : MAX_IMAGE_ATTACHMENT_MB;
  if (file.size <= limitMb * 1024 * 1024) return null;
  return `Couldn’t attach ${file.name || "the file"}: ${video ? "a video" : "an image"} can be at most ${limitMb} MB.`;
}

/** The markdown that embeds an uploaded attachment; a bare video URL renders as a player. */
export function attachmentMarkdown(attachment: PullRequestAttachment, fileName: string): string {
  if (attachment.content_type.startsWith("video/")) return attachment.url;
  const alt = fileName.replace(/\.[^.]+$/, "").replace(/[[\]]/g, "") || "image";
  return `![${alt}](${attachment.url})`;
}

// ── Uploaded image copies ───────────────────────────────────────────────────
// GitHub serves a fresh upload only about 15s after a saved body links it, so the
// images uploaded from this tab show from local copies, keyed by their unique
// asset URL, until the page reloads. Videos render as links and are not kept.

const MAX_UPLOADED_IMAGE_COPIES = 50;
const uploadedImageCopies = new Map<string, string>();

export function rememberUploadedImage(url: string, file: File): void {
  if (!file.type.startsWith("image/")) return;
  uploadedImageCopies.set(url, URL.createObjectURL(file));
  for (const [oldest, copy] of uploadedImageCopies) {
    if (uploadedImageCopies.size <= MAX_UPLOADED_IMAGE_COPIES) break;
    URL.revokeObjectURL(copy);
    uploadedImageCopies.delete(oldest);
  }
}

/** A markdown image, from its local copy when this tab uploaded it; styled like Streamdown's. */
function PullRequestImage({
  node: _node,
  src,
  alt,
  className,
  ...props
}: React.ComponentProps<"img"> & { node?: unknown }) {
  const shown = (typeof src === "string" && uploadedImageCopies.get(src)) || src;
  // Keyed by source, so the same element given a new URL tries it.
  const [failedSrc, setFailedSrc] = useState<string | undefined>(undefined);
  if (shown && failedSrc === shown) {
    return <span className="text-xs text-muted-foreground italic">Image not available</span>;
  }
  return (
    <img
      {...props}
      alt={alt ?? ""}
      src={shown}
      referrerPolicy="no-referrer"
      onError={() => setFailedSrc(shown)}
      className={cn("my-4 inline-block max-w-full rounded-lg", className)}
    />
  );
}

/** Markdown components for PR descriptions and comments. */
export const PULL_REQUEST_MARKDOWN_COMPONENTS: Components = { img: PullRequestImage };

// ── Description drafts ──────────────────────────────────────────────────────
// Kept outside React so a draft, and uploads still landing in it, survive the
// Summary tab unmounting when the reader switches tabs.

interface DescriptionDraft {
  /** Unique per opened editor, so an upload lands only in the draft it started in. */
  id: number;
  body: string;
  uploading: number;
  error: string | null;
}

const descriptionDrafts = new Map<string, DescriptionDraft>();
let lastDraftId = 0;
// Page-wide, so a placeholder never repeats when the editor remounts.
let lastUploadId = 0;
const draftListeners = new Set<() => void>();

function subscribeToDrafts(listener: () => void): () => void {
  draftListeners.add(listener);
  return () => draftListeners.delete(listener);
}

function updateDescriptionDraft(
  key: string,
  update: (draft: DescriptionDraft) => DescriptionDraft | null,
  draftId?: number,
): void {
  const draft = descriptionDrafts.get(key);
  if (!draft || (draftId !== undefined && draft.id !== draftId)) return;
  const next = update(draft);
  if (next) descriptionDrafts.set(key, next);
  else descriptionDrafts.delete(key);
  draftListeners.forEach((listener) => listener());
}

function startDescriptionDraft(key: string, body: string): void {
  lastDraftId += 1;
  descriptionDrafts.set(key, { id: lastDraftId, body, uploading: 0, error: null });
  draftListeners.forEach((listener) => listener());
}

/** Insert `text` over `[start, end)` on a line of its own, as a video URL needs. */
export function insertOnOwnLine(body: string, text: string, start: number, end: number) {
  const before = start > 0 && body[start - 1] !== "\n" ? "\n" : "";
  const after = end < body.length && body[end] !== "\n" ? "\n" : "";
  const inserted = `${before}${text}${after}`;
  return {
    body: `${body.slice(0, start)}${inserted}${body.slice(end)}`,
    caret: start + inserted.length,
  };
}

/** Forget every draft and uploaded image copy; for tests. */
export function resetPullRequestDescriptionDrafts(): void {
  descriptionDrafts.clear();
  lastUploadId = 0;
  uploadedImageCopies.forEach((copy) => URL.revokeObjectURL(copy));
  uploadedImageCopies.clear();
  draftListeners.forEach((listener) => listener());
}

// ── Title ───────────────────────────────────────────────────────────────────

/** The PR title linking out, with a pencil that turns it into an input for editors. */
export function PullRequestTitle({
  conversationId,
  prUrl,
  url,
  title,
  numberLabel,
  editable,
}: {
  conversationId: string;
  /** The selected PR the edit applies to. */
  prUrl: string | null;
  /** The PR's page on the provider. */
  url: string;
  title: string;
  /** e.g. `#17`. */
  numberLabel: string;
  editable: boolean;
}) {
  const update = useUpdatePullRequestDetails(conversationId);
  const [draft, setDraft] = useState<string | null>(null);
  const errorId = useId();

  const save = () => {
    const next = draft?.trim() ?? "";
    if (!prUrl || !next || next === title) {
      setDraft(null);
      update.reset();
      return;
    }
    update.mutate({ prUrl, title: next }, { onSuccess: () => setDraft(null) });
  };
  const cancel = () => {
    setDraft(null);
    update.reset();
  };

  if (draft !== null) {
    return (
      <div className="min-w-0 flex-1">
        <form
          className="flex min-w-0 items-center gap-1.5"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <Input
            autoFocus
            aria-label="Pull request title"
            aria-invalid={update.isError || undefined}
            aria-describedby={update.isError ? errorId : undefined}
            value={draft}
            maxLength={256}
            disabled={update.isPending}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                cancel();
              }
            }}
            className="h-7 min-w-0 flex-1 text-ui font-medium"
          />
          <Button type="submit" size="xs" disabled={update.isPending || !draft.trim()}>
            {update.isPending && <Loader2Icon aria-hidden className="size-3 animate-spin" />}
            {update.isPending ? "Saving…" : "Save"}
          </Button>
          <Button type="button" size="xs" variant="ghost" onClick={cancel}>
            Cancel
          </Button>
        </form>
        {update.isError && (
          <p id={errorId} role="alert" className="mt-1 text-ui text-destructive">
            {update.error.message}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="flex min-w-0 items-center gap-1">
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        className="flex min-w-0 items-center gap-1 text-ui font-medium hover:underline"
      >
        <span className="truncate">{title}</span>
        <span className="shrink-0 text-muted-foreground">{numberLabel}</span>
        <ExternalLinkIcon className="size-3 shrink-0 text-muted-foreground" />
      </a>
      {editable && prUrl && (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Edit title"
          title="Edit title"
          onClick={() => setDraft(title)}
          className="shrink-0 text-muted-foreground"
        >
          <PencilIcon aria-hidden className="size-3" />
        </Button>
      )}
    </div>
  );
}

// ── Description ─────────────────────────────────────────────────────────────

/** The Summary tab's Description section: the rendered body, or its editor. */
export function PullRequestDescription({
  conversationId,
  prUrl,
  body,
  editable,
  attachments,
}: {
  conversationId: string;
  prUrl: string | null;
  body: string | null | undefined;
  editable: boolean;
  /** Images and videos can be uploaded into the body. */
  attachments: boolean;
}) {
  const key = `${conversationId}\n${prUrl ?? ""}`;
  const draft = useSyncExternalStore(subscribeToDrafts, () => descriptionDrafts.get(key) ?? null);
  const canEdit = editable && !!prUrl;

  return (
    <section className="space-y-1.5" aria-labelledby="pull-request-description-heading">
      <div className="flex items-center gap-1">
        <h3
          id="pull-request-description-heading"
          className="text-xs font-medium tracking-wide text-muted-foreground uppercase"
        >
          Description
        </h3>
        {canEdit && !draft && (
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="Edit description"
            title="Edit description"
            onClick={() => startDescriptionDraft(key, body ?? "")}
            className="text-muted-foreground"
          >
            <PencilIcon aria-hidden className="size-3" />
          </Button>
        )}
      </div>
      {canEdit && draft ? (
        <PullRequestDescriptionEditor
          conversationId={conversationId}
          prUrl={prUrl}
          draftKey={key}
          draft={draft}
          original={body ?? ""}
          attachments={attachments}
        />
      ) : body ? (
        <div className="text-ui break-words">
          <MessageResponse
            allowGitHubAttachmentImages
            components={PULL_REQUEST_MARKDOWN_COMPONENTS}
          >
            {body}
          </MessageResponse>
        </div>
      ) : (
        <p className="text-ui text-muted-foreground italic">No description provided.</p>
      )}
    </section>
  );
}

// At most the Summary tab's visible height less the editor's tab row, buttons, and
// scroll margin, so Save stays in view once the editor scrolls itself into view.
const EDITOR_HEIGHT = "min-h-40 max-h-[max(10rem,calc(100cqh-7rem))]";

function PullRequestDescriptionEditor({
  conversationId,
  prUrl,
  draftKey,
  draft,
  original,
  attachments,
}: {
  conversationId: string;
  prUrl: string;
  draftKey: string;
  draft: DescriptionDraft;
  original: string;
  attachments: boolean;
}) {
  const update = useUpdatePullRequestDetails(conversationId);
  // Where an attachment goes: the last caret or selection, or the end before any.
  const selection = useRef<{ start: number; end: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  // Bring Save and Cancel into view when the editor opens low in the Summary tab.
  // Braced: newer browsers return a Promise, which React would call as a cleanup.
  useEffect(() => {
    editorRef.current?.scrollIntoView?.({ block: "nearest" });
  }, []);

  const attach = useCallback(
    (files: File[]) => {
      const attachable = files.filter(isAttachableMedia);
      if (attachable.length < files.length) {
        updateDescriptionDraft(draftKey, (current) => ({
          ...current,
          error: "Only images and videos can be attached.",
        }));
      }
      const sizeErrors = attachable.map(attachmentSizeError);
      const tooLarge = sizeErrors.find((error) => error !== null);
      if (tooLarge) {
        updateDescriptionDraft(draftKey, (current) => ({ ...current, error: tooLarge }));
      }
      const media = attachable.filter((_file, index) => sizeErrors[index] === null);
      const draftId = descriptionDrafts.get(draftKey)?.id;
      if (draftId === undefined) return;
      for (const file of media) {
        lastUploadId += 1;
        const placeholder = `![Uploading ${file.name || "file"}…](#upload-${lastUploadId})`;
        updateDescriptionDraft(draftKey, (current) => {
          const start = Math.min(
            selection.current?.start ?? current.body.length,
            current.body.length,
          );
          const end = Math.min(selection.current?.end ?? start, current.body.length);
          const { body, caret } = insertOnOwnLine(current.body, placeholder, start, end);
          // The next attachment goes after this placeholder, not before it.
          selection.current = { start: caret, end: caret };
          return { ...current, body, uploading: current.uploading + 1 };
        });
        uploadPullRequestAttachment(conversationId, prUrl, file).then(
          (attachment) => {
            rememberUploadedImage(attachment.url, file);
            updateDescriptionDraft(
              draftKey,
              (current) => ({
                ...current,
                body: current.body.replace(placeholder, attachmentMarkdown(attachment, file.name)),
                uploading: current.uploading - 1,
              }),
              draftId,
            );
          },
          (error: unknown) =>
            updateDescriptionDraft(
              draftKey,
              (current) => ({
                ...current,
                body: current.body.replace(placeholder, ""),
                uploading: current.uploading - 1,
                error: `Couldn’t attach ${file.name || "the file"}: ${
                  error instanceof Error ? error.message : String(error)
                }`,
              }),
              draftId,
            ),
        );
      }
    },
    [conversationId, draftKey, prUrl],
  );

  const close = () => {
    update.reset();
    updateDescriptionDraft(draftKey, () => null);
  };
  const save = () => {
    if (draft.body === original) {
      close();
      return;
    }
    update.mutate({ prUrl, body: draft.body }, { onSuccess: close });
  };
  const cancel = () => {
    if (draft.body !== original && !window.confirm("Discard your description changes?")) return;
    close();
  };

  const error = update.isError ? update.error.message : draft.error;
  return (
    <div ref={editorRef} className="scroll-my-3 space-y-2">
      <Tabs defaultValue="write" componentId="github.panel.description.tabs" className="gap-2">
        <div className="flex items-center gap-1">
          <TabsList variant="line" aria-label="Description editor" className="h-auto gap-0 p-0">
            <TabsTrigger value="write" className="flex-none border-0 px-2 leading-none">
              Write
            </TabsTrigger>
            <TabsTrigger value="preview" className="flex-none border-0 px-2 leading-none">
              Preview
            </TabsTrigger>
          </TabsList>
          {attachments && (
            <>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Attach images or videos"
                title="Attach images or videos"
                className="ml-auto text-muted-foreground"
                onClick={() => fileInputRef.current?.click()}
              >
                <PaperclipIcon aria-hidden className="size-3.5" />
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ATTACHMENT_ACCEPT}
                hidden
                data-testid="pull-request-description-file-input"
                onChange={(event) => {
                  attach(Array.from(event.target.files ?? []));
                  event.target.value = "";
                }}
              />
            </>
          )}
        </div>
        <TabsContent value="write">
          <Textarea
            aria-label="Pull request description"
            value={draft.body}
            disabled={update.isPending}
            onSelect={(event) => {
              const { selectionStart, selectionEnd } = event.currentTarget;
              selection.current = { start: selectionStart, end: selectionEnd };
            }}
            onChange={(event) => {
              const { value: body, selectionStart, selectionEnd } = event.target;
              selection.current = { start: selectionStart, end: selectionEnd };
              updateDescriptionDraft(draftKey, (current) => ({ ...current, body, error: null }));
            }}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.files);
              if (!attachments || files.length === 0) return;
              event.preventDefault();
              attach(files);
            }}
            onDragOver={(event) => {
              if (attachments && event.dataTransfer.types.includes("Files")) event.preventDefault();
            }}
            onDrop={(event) => {
              const files = Array.from(event.dataTransfer.files);
              if (!attachments || files.length === 0) return;
              event.preventDefault();
              attach(files);
            }}
            placeholder={
              attachments ? "Markdown. Paste or drop images and videos to attach them." : "Markdown"
            }
            className={cn(EDITOR_HEIGHT, "font-mono text-xs")}
          />
        </TabsContent>
        <TabsContent value="preview">
          <div
            className={cn(
              EDITOR_HEIGHT,
              "overflow-y-auto rounded-lg border border-border px-2.5 py-2 text-ui break-words",
            )}
          >
            {draft.body.trim() ? (
              <MessageResponse
                allowGitHubAttachmentImages
                components={PULL_REQUEST_MARKDOWN_COMPONENTS}
              >
                {draft.body}
              </MessageResponse>
            ) : (
              <p className="text-muted-foreground italic">Nothing to preview.</p>
            )}
          </div>
        </TabsContent>
      </Tabs>
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error}
        </p>
      )}
      <div className="flex items-center justify-end gap-1.5">
        {draft.uploading > 0 && (
          <span
            role="status"
            className="mr-auto flex items-center gap-1 text-xs text-muted-foreground"
          >
            <Loader2Icon aria-hidden className="size-3 animate-spin" />
            Uploading {draft.uploading} {draft.uploading === 1 ? "file" : "files"}…
          </span>
        )}
        <Button size="xs" variant="ghost" onClick={cancel} disabled={update.isPending}>
          Cancel
        </Button>
        <Button size="xs" onClick={save} disabled={update.isPending || draft.uploading > 0}>
          {update.isPending && <Loader2Icon aria-hidden className="size-3 animate-spin" />}
          {update.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}
