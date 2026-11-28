// The PR's title (in the panel header) and description (in the Summary tab), each
// editable in place by editors. The description editor is a raw markdown textarea
// with a preview; pasted, dropped, or picked images and videos upload at once and
// are embedded as provider URLs, so saving is one plain text edit.

import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { Loader2Icon, PaperclipIcon, PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { Components } from "streamdown";
import { MessageResponse } from "@/components/ai-elements/message";
import { PULL_REQUEST_REFERENCE_COMPONENTS } from "@/shell/pullRequestReferences/PullRequestReferenceLink";
import { LIGHTBOX_GALLERY_PROPS } from "@/lib/lightboxGallery";
import { cn } from "@/lib/utils";
import { closeLightboxShowing, ZoomableImage } from "@/components/ImageLightbox";
import { loadImageSource } from "@/lib/imageSource";
import { useUpdatePullRequestDetails } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  ATTACHMENT_ACCEPT,
  isAttachableMedia,
  resetUploadIdsForTests,
  uploadAttachments,
  uploadPlaceholderMarkdown,
} from "@/markdownEditor/pullRequestAttachments";
import { MarkdownComposer } from "@/markdownEditor/MarkdownComposer";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import {
  PullRequestPageImage,
  usePullRequestImagePage,
} from "@/pullRequestPage/PullRequestPageImage";

import {
  usePullRequestSectionStyle,
  usePullRequestSectionTitleClass,
} from "@/shell/pullRequestSectionStyle";
import {
  type StackDescriptionSegments,
  stackDescriptionSegments,
} from "@/lib/pullRequestStacks/stackDescription";
import {
  editableStackSuffix,
  keptStackParse,
  STACK_INFO_UPDATED,
  stackAfterSave,
  stackDraftAfterChange,
  stackDraftApplyingMine,
  withStackSuffix,
} from "@/lib/pullRequestStacks/stackDescriptionDraft";
import { stackPullRequestRef, type VerifiedStackParse } from "@/lib/pullRequestStacks/stackModel";
import { PullRequestStackCard } from "@/pullRequestPage/PullRequestStack";
import {
  pullRequestStackView,
  type PullRequestStackView,
} from "@/pullRequestPage/usePullRequestStack";
import { descriptionMetadataEditable } from "@/lib/pullRequestStacks/stackTrust";
import { GITHUB_MARKDOWN_REHYPE_PLUGINS } from "@/lib/githubMarkdownPlugins";

// ── Uploaded media copies ───────────────────────────────────────────────────
// GitHub serves a fresh upload only about 15s after a saved body links it, so the
// images and videos uploaded from this tab show from local copies, keyed by their
// unique asset URL, until the page reloads.

const MAX_UPLOADED_MEDIA_COPIES = 50;
const uploadedMediaCopies = new Map<string, string>();

/**
 * Keeps a local copy of an uploaded image or video. An image whose type can't be previewed
 * safely gets none; a video plays from its file as it is.
 */
export async function rememberUploadedImage(url: string, file: File): Promise<void> {
  if (!isAttachableMedia(file)) return;
  let copy: string;
  if (file.type.startsWith("video/")) {
    copy = URL.createObjectURL(file);
  } else {
    try {
      copy = (await loadImageSource(file, { path: file.name })).url;
    } catch {
      return;
    }
  }
  uploadedMediaCopies.set(url, copy);
  for (const [oldest, evicted] of uploadedMediaCopies) {
    if (uploadedMediaCopies.size <= MAX_UPLOADED_MEDIA_COPIES) break;
    closeLightboxShowing(evicted);
    URL.revokeObjectURL(evicted);
    uploadedMediaCopies.delete(oldest);
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
  const shown = (typeof src === "string" && uploadedMediaCopies.get(src)) || src;
  // Keyed by source, so the same element given a new URL tries it.
  const [failedSrc, setFailedSrc] = useState<string | undefined>(undefined);
  // The new page loads GitHub attachments of private repositories through this Mac.
  const pagePrUrl = usePullRequestImagePage();
  if (pagePrUrl && typeof shown === "string" && shown === src) {
    return (
      <PullRequestPageImage
        prUrl={pagePrUrl}
        src={shown}
        alt={alt ?? ""}
        className={className}
        imageProps={props}
      />
    );
  }
  if (shown && failedSrc === shown) {
    return <span className="text-xs text-muted-foreground italic">Image not available</span>;
  }
  return (
    <ZoomableImage
      {...props}
      alt={alt ?? ""}
      src={shown}
      referrerPolicy="no-referrer"
      onError={() => setFailedSrc(shown)}
      buttonClassName="my-4"
      className={cn("inline-block max-w-full rounded-lg", className)}
    />
  );
}

/**
 * A bare attachment URL GitHub plays as a video, from its local copy when this tab
 * uploaded it. One that won't play, e.g. an image or a not-yet-served upload, stays
 * the link it was before.
 */
function PullRequestVideo({
  node: _node,
  src,
  className,
  ...props
}: React.ComponentProps<"video"> & { node?: unknown }) {
  const shown = (src && uploadedMediaCopies.get(src)) || src;
  const [failedSrc, setFailedSrc] = useState<string | undefined>(undefined);
  if (src && failedSrc === shown) {
    return (
      <a
        href={src}
        target="_blank"
        rel="noreferrer"
        data-streamdown="link"
        className="wrap-anywhere font-medium text-primary underline"
      >
        {src}
      </a>
    );
  }
  return (
    <video
      {...props}
      src={shown}
      onError={() => setFailedSrc(shown)}
      className={cn("my-4 block max-h-160 max-w-full rounded-lg", className)}
    />
  );
}

/** Markdown components for PR descriptions and comments: images and videos, and PR references with status. */
export const PULL_REQUEST_MARKDOWN_COMPONENTS: Components = {
  ...PULL_REQUEST_REFERENCE_COMPONENTS,
  img: PullRequestImage,
  video: PullRequestVideo,
};

/** A description or comment draft rendered as GitHub will show it once posted. */
export function PullRequestMarkdownPreview({ body }: { body: string }) {
  return (
    <MessageResponse
      allowGitHubAttachmentImages
      rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
      components={PULL_REQUEST_MARKDOWN_COMPONENTS}
    >
      {body}
    </MessageResponse>
  );
}

// ── Description drafts ──────────────────────────────────────────────────────
// Kept outside React so a draft, and uploads still landing in it, survive the
// Summary tab unmounting when the reader switches tabs.

interface DescriptionDraft {
  /** Unique per opened editor, so an upload lands only in the draft it started in. */
  id: number;
  /** The text being edited: the description, or the title. */
  body: string;
  /** The value the editor started from; a change on this Mac needs GitHub to still have it. */
  expected: string;
  uploading: number;
  error: string | null;
  /** GitHub has another value now than `expected`: the user picks theirs or keeps theirs. */
  conflict?: string;
  /** Saved on this Mac before the workspace user switched away: it may be on GitHub. */
  abandoned?: boolean;
  /** A save of this draft is under way, on either path; kept if its editor remounts. */
  saving?: boolean;
  /**
   * Set while only the text after a verified stack section is edited: the segments of the
   * description `expected` names. The body is their text above and section, then the edit.
   */
  stack?: StackDescriptionSegments;
  /** Something the draft's editor says about what happened to it, such as a kept edit. */
  notice?: string;
}

/** Where a field's editor keeps its text: per workspace user, session, PR and field. */
function detailsDraftKey(
  owner: string,
  conversationId: string,
  prUrl: string | null,
  field: "title" | "body",
): string {
  return [owner, conversationId, prUrl ?? "", field].join("\n");
}

const descriptionDrafts = new Map<string, DescriptionDraft>();
let lastDraftId = 0;
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

function startDescriptionDraft(key: string, body: string, stack?: StackDescriptionSegments): void {
  lastDraftId += 1;
  descriptionDrafts.set(key, {
    id: lastDraftId,
    body,
    expected: body,
    uploading: 0,
    error: null,
    ...(stack ? { stack } : {}),
  });
  draftListeners.forEach((listener) => listener());
}

/** A field editor's draft, re-read when it changes; null while the editor is closed. */
function useDetailsDraft(key: string): DescriptionDraft | null {
  return useSyncExternalStore(subscribeToDrafts, () => descriptionDrafts.get(key) ?? null);
}

/**
 * Save a field from its open draft as `expected` says GitHub has it, and handle what
 * comes back for that draft only, even after its editor unmounted: closed on success, an
 * error or a conflict kept with the draft, and, if the workspace user switches away
 * first, marked as maybe saved. Refused, changing nothing, while an upload is still
 * going or a title or description change to the PR is still being made on this Mac.
 */
function saveDetailsDraft(
  update: ReturnType<typeof useUpdatePullRequestDetails>,
  {
    key,
    draft,
    prUrl,
    field,
    value,
    expected = draft.expected,
    onChanged = (current, now) => ({ ...current, conflict: now }),
  }: {
    key: string;
    draft: DescriptionDraft;
    prUrl: string;
    field: "title" | "body";
    value: string;
    expected?: string;
    /** What the draft becomes when GitHub has `now` instead of `expected`. */
    onChanged?: (current: DescriptionDraft, now: string) => DescriptionDraft;
  },
): void {
  if (draft.saving || draft.uploading > 0) return;
  updateDescriptionDraft(
    key,
    (current) => ({
      ...current,
      expected,
      conflict: undefined,
      abandoned: false,
      error: null,
      notice: undefined,
      saving: true,
    }),
    draft.id,
  );
  update.mutate(
    // A draft that keeps a stack section is only saved where GitHub's text is checked first.
    { prUrl, field, value, expected, ...(draft.stack ? { requiresExpectedCheck: true } : {}) },
    {
      // Closed only if it still holds what was saved; newer text stays, over the saved value.
      onSuccess: () =>
        updateDescriptionDraft(
          key,
          (current) =>
            current.body === draft.body
              ? null
              : {
                  ...current,
                  expected: value,
                  saving: false,
                  error: null,
                  ...(current.stack ? { stack: stackAfterSave(current.stack, value) } : {}),
                },
          draft.id,
        ),
      onError: (error) => {
        const now =
          error instanceof PullRequestWriteError && error.current?.field === field
            ? error.current.value
            : undefined;
        // Kept with the draft, so an editor opened again shows it too.
        updateDescriptionDraft(
          key,
          (current) =>
            now === undefined
              ? { ...current, error: error.message, saving: false }
              : { ...onChanged(current, now), error: null, saving: false },
          draft.id,
        );
      },
    },
  );
}

/**
 * What a field editor says about its last save on this Mac: GitHub has another value
 * since editing began (Use theirs or Apply mine), or it may have been saved before the
 * workspace user switched accounts.
 */
function DetailsEditNotices({
  draft,
  prUrl,
  field,
  onUseTheirs,
  onApplyMine,
  onDismiss,
  saving,
}: {
  draft: DescriptionDraft;
  prUrl: string;
  field: "title" | "body";
  onUseTheirs: () => void;
  onApplyMine: () => void;
  onDismiss: () => void;
  /** A save is under way, or must wait for one or an upload; Apply mine waits too. */
  saving: boolean;
}) {
  const what = field === "title" ? "title" : "description";
  if (draft.conflict !== undefined) {
    return (
      <div role="alert" className="space-y-1.5 rounded-md border border-border p-2 text-ui">
        <p>This {what} changed on GitHub since you started editing.</p>
        <p className="line-clamp-3 break-words text-muted-foreground">
          {draft.conflict.trim() ? draft.conflict : `The ${what} on GitHub is empty.`}
        </p>
        <div className="flex gap-1.5">
          <Button size="xs" variant="outline" onClick={onUseTheirs}>
            Use theirs
          </Button>
          <Button size="xs" onClick={onApplyMine} disabled={saving}>
            Apply mine
          </Button>
        </div>
      </div>
    );
  }
  if (!draft.abandoned) return null;
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui">
      <p className="min-w-0 text-muted-foreground">
        This may have been saved before you switched accounts. Check the pull request on GitHub
        before saving again.
      </p>
      <Button asChild variant="link" size="sm" className="h-auto p-0">
        <a href={prUrl} target="_blank" rel="noreferrer">
          Open on GitHub
        </a>
      </Button>
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto p-0 text-muted-foreground"
        onClick={onDismiss}
      >
        Dismiss
      </Button>
    </div>
  );
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

/** Forget every title and description draft and uploaded image copy; for tests. */
export function resetPullRequestDescriptionDrafts(): void {
  descriptionDrafts.clear();
  resetUploadIdsForTests();
  uploadedMediaCopies.forEach((copy) => URL.revokeObjectURL(copy));
  uploadedMediaCopies.clear();
  draftListeners.forEach((listener) => listener());
}

// ── Title ───────────────────────────────────────────────────────────────────

/** The PR title linking out, with a pencil that turns it into an input for editors. */
export function PullRequestTitle({
  conversationId,
  prUrl,
  title,
  editable,
  size = "panel",
}: {
  conversationId: string;
  /** The selected PR the edit applies to. */
  prUrl: string | null;
  title: string;
  editable: boolean;
  /** "page" is the page's main heading, with a labelled Edit button. */
  size?: "panel" | "page";
}) {
  const update = useUpdatePullRequestDetails(conversationId);
  const owner = useWorkspaceDraftOwner();
  const key = detailsDraftKey(owner, conversationId, prUrl, "title");
  const draft = useDetailsDraft(key);
  const errorId = useId();
  const saving = update.isPending || !!draft?.saving;

  const close = () => {
    update.reset();
    updateDescriptionDraft(key, () => null);
  };
  const save = (expected?: string) => {
    if (!draft) return;
    const next = draft.body.trim();
    if (!prUrl || !next || (next === draft.expected && expected === undefined)) {
      close();
      return;
    }
    saveDetailsDraft(update, { key, draft, prUrl, field: "title", value: next, expected });
  };
  const cancel = close;
  const setTitle = (value: string) =>
    updateDescriptionDraft(key, (current) => ({ ...current, body: value, abandoned: false }));

  if (draft !== null && prUrl) {
    const error = draft.error;
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
            aria-invalid={!!error || undefined}
            aria-describedby={error ? errorId : undefined}
            value={draft.body}
            maxLength={256}
            disabled={saving}
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                cancel();
              }
            }}
            className={cn(
              "min-w-0 flex-1 font-medium",
              size === "page" ? "h-9 text-lg" : "h-7 text-ui",
            )}
          />
          <Button type="submit" size="xs" disabled={saving || !draft.body.trim()}>
            {saving && <Loader2Icon aria-hidden className="size-3 animate-spin" />}
            {saving ? "Saving…" : "Save"}
          </Button>
          <Button type="button" size="xs" variant="ghost" onClick={cancel}>
            Cancel
          </Button>
        </form>
        {error && (
          <p id={errorId} role="alert" className="mt-1 text-ui text-destructive">
            {error}
          </p>
        )}
        <div className="mt-1">
          <DetailsEditNotices
            draft={draft}
            prUrl={prUrl}
            field="title"
            onUseTheirs={close}
            onApplyMine={() => save(draft.conflict)}
            saving={saving}
            onDismiss={() =>
              updateDescriptionDraft(key, (current) => ({ ...current, abandoned: false }))
            }
          />
        </div>
      </div>
    );
  }

  if (size === "page") {
    return (
      // A narrow page puts Edit under the title, as the reference does.
      <div className="flex min-w-0 items-start gap-4 @max-[420px]:flex-col @max-[420px]:gap-1.5">
        <PullRequestPageTitleHeading>{title}</PullRequestPageTitleHeading>
        {editable && prUrl && (
          <Button
            variant="ghost"
            size="sm"
            aria-label="Edit title"
            onClick={() => startDescriptionDraft(key, title)}
            className="shrink-0 text-muted-foreground @max-[420px]:-ml-2"
          >
            <PencilIcon aria-hidden className="size-3.5" />
            Edit
          </Button>
        )}
      </div>
    );
  }
  return (
    <div className="flex min-w-0 items-start gap-1">
      {/* The full title wraps; the header's repository link opens the PR. */}
      <h3 className="min-w-0 text-base leading-snug font-semibold break-words text-foreground">
        {title}
      </h3>
      {editable && prUrl && (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Edit title"
          title="Edit title"
          onClick={() => startDescriptionDraft(key, title)}
          className="mt-0.5 shrink-0 text-muted-foreground"
        >
          <PencilIcon aria-hidden className="size-3" />
        </Button>
      )}
    </div>
  );
}

/** A PR's title as the new page heads its Overview with it, edited or not. */
export function PullRequestPageTitleHeading({ children }: { children: ReactNode }) {
  return (
    <h1 className="min-w-0 flex-1 text-2xl leading-tight font-bold tracking-tight break-words text-foreground">
      {children}
    </h1>
  );
}

// ── Description ─────────────────────────────────────────────────────────────

/** The Summary tab's Description section: the rendered body, or its editor. */
export function PullRequestDescription({
  conversationId,
  prUrl,
  body,
  stack = null,
  editable,
  attachments,
  attachmentsNeedHost = false,
}: {
  conversationId: string;
  prUrl: string | null;
  body: string | null | undefined;
  /** The stack the body lists; a verified one shows as a card in place of its section. */
  stack?: PullRequestStackView | null;
  editable: boolean;
  /** Images and videos can be uploaded into the body. */
  attachments: boolean;
  /** Saved on this Mac, while uploads still go through the session's machine, which can't now. */
  attachmentsNeedHost?: boolean;
}) {
  const owner = useWorkspaceDraftOwner();
  const sectionStyle = usePullRequestSectionStyle();
  const titleClass = usePullRequestSectionTitleClass();
  const key = detailsDraftKey(owner, conversationId, prUrl, "body");
  const draft = useDetailsDraft(key);
  const canEdit = editable && !!prUrl;
  // The session's host doesn't check GitHub's text before saving, so the editor edits it all.
  const startEditing = () => startDescriptionDraft(key, body ?? "", undefined);

  return (
    // Every image in the description, stack sections included, is one gallery.
    <section
      {...LIGHTBOX_GALLERY_PROPS}
      className="space-y-1.5"
      aria-labelledby="pull-request-description-heading"
    >
      {/* On a page the row keeps Edit's height, so it lays out the same before it can edit. */}
      <div
        className={cn(
          "flex items-center gap-1",
          sectionStyle !== "panel" && "min-h-7 justify-between",
        )}
      >
        <h3 id="pull-request-description-heading" className={titleClass}>
          Description
        </h3>
        {canEdit &&
          !draft &&
          (sectionStyle === "panel" ? (
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Edit description"
              title="Edit description"
              onClick={startEditing}
              className="text-muted-foreground"
            >
              <PencilIcon aria-hidden className="size-3" />
            </Button>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              aria-label="Edit description"
              onClick={startEditing}
              className="text-muted-foreground"
            >
              <PencilIcon aria-hidden className="size-3.5" />
              Edit
            </Button>
          ))}
      </div>
      {canEdit && draft ? (
        <PullRequestDescriptionEditor
          conversationId={conversationId}
          prUrl={prUrl}
          draftKey={key}
          draft={draft}
          stack={stack}
          attachments={attachments}
          attachmentsNeedHost={attachmentsNeedHost}
        />
      ) : body && stack && descriptionMetadataEditable(stack.parse) ? (
        <StackedDescription body={body} stack={stack} parse={stack.parse} />
      ) : stack?.parse.trust === "native" ? (
        // GitHub's own stack leads; the description below stays exactly as written.
        <>
          <PullRequestStackCard stack={stack} />
          {body ? (
            <DescriptionMarkdown body={body} />
          ) : (
            <p className="text-ui text-muted-foreground italic">No description provided.</p>
          )}
        </>
      ) : body ? (
        <DescriptionMarkdown body={body} />
      ) : (
        <p className="text-ui text-muted-foreground italic">No description provided.</p>
      )}
    </section>
  );
}

function DescriptionMarkdown({ body }: { body: string }) {
  return (
    <div className="text-ui break-words">
      <MessageResponse
        allowGitHubAttachmentImages
        rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
        components={PULL_REQUEST_MARKDOWN_COMPONENTS}
      >
        {body}
      </MessageResponse>
    </div>
  );
}

/** A description whose stack section reads as the stack card, the text around it as written. */
function StackedDescription({
  body,
  stack,
  parse,
}: {
  body: string;
  stack: PullRequestStackView;
  parse: VerifiedStackParse;
}) {
  const { prefix, suffix } = stackDescriptionSegments(body, parse);
  return (
    <>
      {prefix.trim() && <DescriptionMarkdown body={prefix} />}
      <PullRequestStackCard stack={stack} />
      {suffix.trim() && <DescriptionMarkdown body={suffix} />}
    </>
  );
}

/**
 * Above an editor that keeps a stack section: the text above the section and the stack, both
 * as the draft will save them, and the way to edit the whole description instead.
 */
function StackSectionKept({
  prUrl,
  segments,
  stack,
  onEditFullDescription,
}: {
  prUrl: string;
  segments: StackDescriptionSegments;
  /** The stack as the page last read it: only its members' statuses are shown. */
  stack: PullRequestStackView | null;
  onEditFullDescription: () => void;
}) {
  const kept = useMemo(() => {
    const self = stackPullRequestRef(prUrl);
    const parse = self && keptStackParse(segments, self);
    return (
      parse &&
      pullRequestStackView(
        parse,
        stack?.members ?? { kind: "unavailable" },
        stack?.selectInPlace ?? (() => false),
      )
    );
  }, [prUrl, segments, stack?.members, stack?.selectInPlace]);
  return (
    <div className="space-y-1.5" data-stack-section-kept="">
      {segments.prefix.trim() && (
        <div className="rounded-md border border-border px-2.5 py-1.5">
          <p className="text-xs text-muted-foreground">Text above the stack info</p>
          <DescriptionMarkdown body={segments.prefix} />
        </div>
      )}
      {kept && <PullRequestStackCard stack={kept} />}
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>The stack info stays as it is. You’re editing the text below it.</span>
        <Button size="xs" variant="ghost" onClick={onEditFullDescription}>
          Edit full description
        </Button>
      </div>
    </div>
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
  stack,
  attachments,
  attachmentsNeedHost,
}: {
  conversationId: string;
  prUrl: string;
  draftKey: string;
  draft: DescriptionDraft;
  /** The stack the description lists, shown above an editor that keeps its section. */
  stack: PullRequestStackView | null;
  attachments: boolean;
  attachmentsNeedHost: boolean;
}) {
  const update = useUpdatePullRequestDetails(conversationId);
  // The new pull request page edits in the Markdown composer; the classic page keeps the textarea.
  const pageV2 = usePullRequestImagePage() !== null;
  // Only the composer edits around a stack section; the classic textarea edits the whole body.
  const stackSegments = pageV2 ? draft.stack : undefined;
  const self = stackPullRequestRef(prUrl);
  // Where an attachment goes: the last caret or selection, or the end before any.
  const selection = useRef<{ start: number; end: number } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const editorRef = useRef<HTMLDivElement>(null);
  // Bring Save and Cancel into view when the editor opens low in the Summary tab.
  // Braced: newer browsers return a Promise, which React would call as a cleanup.
  useEffect(() => {
    editorRef.current?.scrollIntoView?.({ block: "nearest" });
  }, []);
  // While someone types, the editor grows; keep Save and Cancel in view in a short pane.
  const actionsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const editorRoot = editorRef.current;
    if (!editorRoot || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (editorRoot.contains(document.activeElement)) {
        actionsRef.current?.scrollIntoView?.({ block: "nearest" });
      }
    });
    observer.observe(editorRoot);
    return () => observer.disconnect();
  }, []);

  const attach = useCallback(
    (files: File[]) => {
      const draftId = descriptionDrafts.get(draftKey)?.id;
      if (draftId === undefined) return;
      const placeholders = new Map<string, string>();
      uploadAttachments(
        files,
        { conversationId, prUrl, rememberUploadedImage },
        {
          refuse: (error) => updateDescriptionDraft(draftKey, (current) => ({ ...current, error })),
          start: (id, fileName) => {
            const placeholder = uploadPlaceholderMarkdown(id, fileName);
            placeholders.set(id, placeholder);
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
          },
          finish: (id, markdown) =>
            updateDescriptionDraft(
              draftKey,
              (current) => ({
                ...current,
                body: current.body.replace(placeholders.get(id) ?? "", markdown),
                uploading: current.uploading - 1,
              }),
              draftId,
            ),
          fail: (id, error) =>
            updateDescriptionDraft(
              draftKey,
              (current) => ({
                ...current,
                body: current.body.replace(placeholders.get(id) ?? "", ""),
                uploading: current.uploading - 1,
                error,
              }),
              draftId,
            ),
        },
      );
    },
    [conversationId, draftKey, prUrl],
  );

  const close = () => {
    update.reset();
    updateDescriptionDraft(draftKey, () => null);
  };
  // Another description on GitHub: a stack tool's new section moves the edit onto it, and
  // a section no longer verified hands the whole description back for the person to judge.
  const onChanged = (current: DescriptionDraft, now: string): DescriptionDraft => {
    if (!current.stack || !self) return { ...current, conflict: now };
    const change = stackDraftAfterChange(current.stack, current.body, now, self);
    switch (change.kind) {
      case "rebased":
        return {
          ...current,
          body: change.body,
          expected: change.expected,
          stack: change.stack,
          notice: STACK_INFO_UPDATED,
        };
      case "conflict":
        return { ...current, conflict: now };
      case "conflict-full-body":
        return { ...current, stack: undefined, conflict: now };
    }
  };
  const save = (expected?: string, next: DescriptionDraft = draft) => {
    if (next.body === next.expected && expected === undefined) {
      close();
      return;
    }
    saveDetailsDraft(update, {
      key: draftKey,
      draft: next,
      prUrl,
      field: "body",
      value: next.body,
      expected,
      onChanged,
    });
  };
  // Keeping mine over GitHub's: around a stack section, mine is the text after it.
  const applyMine = () => {
    const now = draft.conflict;
    if (now === undefined) return;
    const mine =
      draft.stack && self ? stackDraftApplyingMine(draft.stack, draft.body, now, self) : null;
    if (!mine) {
      save(now);
      return;
    }
    const next = { ...draft, ...mine };
    updateDescriptionDraft(draftKey, () => next, draft.id);
    save(now, next);
  };
  const editedText = stackSegments ? editableStackSuffix(draft.body, stackSegments) : draft.body;
  // The classic textarea edits the whole description: a draft opened around a stack section
  // there stops keeping it, so nothing reads its text as only the part after the section.
  useEffect(() => {
    if (!pageV2 && draft.stack) {
      updateDescriptionDraft(draftKey, (current) => ({ ...current, stack: undefined }), draft.id);
    }
  }, [pageV2, draft.stack, draft.id, draftKey]);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const cancel = () => {
    if (pageV2) {
      // A save under way keeps its draft, so a failed save can still be retried.
      if (saving) return;
      if (draft.body !== draft.expected) setConfirmingDiscard(true);
      else close();
      return;
    }
    if (draft.body !== draft.expected && !window.confirm("Discard your description changes?")) {
      return;
    }
    close();
  };

  const error = draft.error;
  const saving = update.isPending || !!draft?.saving;
  return (
    // Named so a page can make room while the description is being edited.
    <div ref={editorRef} data-pull-request-description-editor className="scroll-my-3 space-y-2">
      {stackSegments && (
        <StackSectionKept
          prUrl={prUrl}
          segments={stackSegments}
          stack={stack}
          onEditFullDescription={() =>
            updateDescriptionDraft(draftKey, (current) => ({ ...current, stack: undefined }))
          }
        />
      )}
      {draft.notice && (
        <p role="status" className="text-ui text-muted-foreground">
          {draft.notice}
        </p>
      )}
      {pageV2 ? (
        <MarkdownComposer
          value={editedText}
          onChange={(text) =>
            updateDescriptionDraft(draftKey, (current) => ({
              ...current,
              body: current.stack ? withStackSuffix(current.stack, text) : text,
              error: null,
              abandoned: false,
            }))
          }
          onSubmit={() => {
            if (!saving && draft.uploading === 0) save();
          }}
          onCancel={cancel}
          submitLabel="Save Description"
          submitVerb="save"
          size="document"
          label="Pull request description"
          placeholder="Describe the change"
          disabled={saving}
          autoFocus
          preview={<PullRequestMarkdownPreview body={editedText} />}
          attachments={
            attachments
              ? {
                  conversationId,
                  prUrl,
                  rememberUploadedImage,
                  // Gated on this draft's id: a late upload never lands in a draft opened since.
                  updateDraftText: (change) =>
                    updateDescriptionDraft(
                      draftKey,
                      (current) => ({
                        ...current,
                        body: current.stack
                          ? withStackSuffix(
                              current.stack,
                              change(editableStackSuffix(current.body, current.stack)),
                            )
                          : change(current.body),
                      }),
                      draft.id,
                    ),
                  onUploadingChange: (delta) =>
                    updateDescriptionDraft(
                      draftKey,
                      (current) => ({ ...current, uploading: current.uploading + delta }),
                      draft.id,
                    ),
                  onAttachmentError: (attachmentError) =>
                    updateDescriptionDraft(
                      draftKey,
                      (current) => ({ ...current, error: attachmentError }),
                      draft.id,
                    ),
                }
              : null
          }
          attachmentsUnavailableReason={
            attachmentsNeedHost ? "Attaching files needs the session’s machine" : undefined
          }
        />
      ) : (
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
            {!attachments && attachmentsNeedHost && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label="Attach images or videos"
                title="Attaching files needs the session’s machine"
                className="ml-auto text-muted-foreground"
                disabled
              >
                <PaperclipIcon aria-hidden className="size-3.5" />
              </Button>
            )}
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
              disabled={saving}
              onSelect={(event) => {
                const { selectionStart, selectionEnd } = event.currentTarget;
                selection.current = { start: selectionStart, end: selectionEnd };
              }}
              onChange={(event) => {
                const { value: body, selectionStart, selectionEnd } = event.target;
                selection.current = { start: selectionStart, end: selectionEnd };
                updateDescriptionDraft(draftKey, (current) => ({
                  ...current,
                  body,
                  error: null,
                  abandoned: false,
                }));
              }}
              onPaste={(event) => {
                const files = Array.from(event.clipboardData.files);
                if (!attachments || files.length === 0) return;
                event.preventDefault();
                attach(files);
              }}
              onDragOver={(event) => {
                if (attachments && event.dataTransfer.types.includes("Files"))
                  event.preventDefault();
              }}
              onDrop={(event) => {
                const files = Array.from(event.dataTransfer.files);
                if (!attachments || files.length === 0) return;
                event.preventDefault();
                attach(files);
              }}
              placeholder={
                attachments
                  ? "Markdown. Paste or drop images and videos to attach them."
                  : "Markdown"
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
                  rehypePluginsAfterSanitizing={GITHUB_MARKDOWN_REHYPE_PLUGINS}
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
      )}
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error}
        </p>
      )}
      <DetailsEditNotices
        draft={draft}
        prUrl={prUrl}
        field="body"
        onUseTheirs={close}
        onApplyMine={applyMine}
        saving={saving || draft.uploading > 0}
        onDismiss={() =>
          updateDescriptionDraft(draftKey, (current) => ({ ...current, abandoned: false }))
        }
      />
      <div ref={actionsRef} className="flex items-center justify-end gap-1.5">
        {draft.uploading > 0 && (
          <span
            role="status"
            className="mr-auto flex items-center gap-1 text-xs text-muted-foreground"
          >
            <Loader2Icon aria-hidden className="size-3 animate-spin" />
            Uploading {draft.uploading} {draft.uploading === 1 ? "file" : "files"}…
          </span>
        )}
        <Button size="xs" variant="ghost" onClick={cancel} disabled={saving}>
          Cancel
        </Button>
        <Button size="xs" onClick={() => save()} disabled={saving || draft.uploading > 0}>
          {saving && <Loader2Icon aria-hidden className="size-3 animate-spin" />}
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>
      <Dialog open={confirmingDiscard} onOpenChange={setConfirmingDiscard}>
        <DialogContent showCloseButton={false} className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>Discard your description changes?</DialogTitle>
            <DialogDescription>The description goes back to what GitHub has.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmingDiscard(false)}>
              Keep Editing
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmingDiscard(false);
                close();
              }}
            >
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
