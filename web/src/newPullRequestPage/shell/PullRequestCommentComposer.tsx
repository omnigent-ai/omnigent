import type {
  PendingComposer,
  PendingComposerNotice,
} from "@/pullRequestReviews/usePendingComposer";
import { type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  GitPullRequestArrowIcon,
  GitPullRequestClosedIcon,
  MessageSquarePlusIcon,
  XIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  MAX_PULL_REQUEST_COMMENT_CHARACTERS,
  pullRequestCommentTooLong,
  usePullRequestCommentComposer,
  type CommentMarkdownBinding,
  type PullRequestComment,
  type PullRequestCommentDraft,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { MarkdownComposer } from "@/markdownEditor/MarkdownComposer";
import { usePullRequestAttachmentsAvailability } from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { usePullRequestImagePage } from "@/pullRequestPage/PullRequestPageImage";
import { useCommentBoxOpenRequests } from "@/lib/commentBoxOpenRequests";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import { createLastFocusedScope } from "@/hooks/lastFocusedScope";
import {
  type PullRequestStateAction,
  usePullRequestStateAction,
} from "@/pullRequestPage/usePullRequestStateAction";
import type { CommentWritesProvider } from "@/pullRequestComments/commentWritesRecovery";
import {
  PullRequestCommentWritesNotice,
  useCommentWritesRecovery,
} from "@/pullRequestComments/PullRequestCommentWritesNotice";
import { gitProviderCopy } from "@/lib/gitProviders";
import { foldedSummary, foldsAfterPost, opensExpanded } from "../../shell/commentFormExpansion";
import { PullRequestMarkdownPreview, rememberUploadedImage } from "./PullRequestDetails";

const NO_PALETTE_COMMANDS: PaletteCommand[] = [];
// The open comment boxes; only the one worked in last offers ⌘K's Cancel Comment.
const commentBoxes = createLastFocusedScope();
const GITHUB_COPY = gitProviderCopy("github");

/** A notice action's ⌘K icon. */
export function PullRequestCommentComposer({
  conversationId,
  prUrl,
  account,
  onPosted,
  active = true,
  prState,
  writesBlocked = false,
  provider = GITHUB_COPY,
}: {
  conversationId: string;
  prUrl: string;
  account: string | null;
  /** Hears of each comment once it shows in the PR's comments. */
  onPosted?: (comment: PullRequestComment) => void;
  /** Whether this box's panel is the one shown, so only it offers ⌘K rows. */
  active?: boolean;
  /** The PR's state as the panel read it, for Close and Reopen Pull Request. */
  prState?: string | null;
  /** Neither this Mac nor the session's machine can post here now; the box explains why. */
  writesBlocked?: boolean;
  /** The PR's provider, as the panel names it. */
  provider?: CommentWritesProvider;
}) {
  const { draft, setBody, post, postWithOutcome, markdown, boxName } =
    usePullRequestCommentComposer(conversationId, prUrl, undefined, { onPosted });
  const recovery = useCommentWritesRecovery({ blocked: writesBlocked, provider });
  const stateAction = usePullRequestStateAction({
    conversationId,
    prUrl,
    prState,
    post: postWithOutcome,
  });
  return (
    <PullRequestCommentForm
      draft={draft}
      setBody={setBody}
      post={post}
      account={account}
      provider={provider}
      markdown={markdown}
      boxName={boxName}
      paletteActive={active}
      stateAction={stateAction}
      postBlocked={!!recovery?.blocksPosting}
      writesNotice={
        recovery && <PullRequestCommentWritesNotice recovery={recovery} prUrl={prUrl} />
      }
    />
  );
}

/** Who posts, named by the PR's provider: an Azure DevOps PR has no GitHub account. */
function commentAuthor(account: string | null, provider: CommentWritesProvider): string {
  if (account) return `Comment as ${account}`;
  return provider.id
    ? `Comment using the host’s ${provider.label} account`
    : "Comment using the host’s account";
}

/** Who posts, for the Comment button and the folded box. */
function commentAttribution(account: string | null, provider: CommentWritesProvider): string {
  return commentAuthor(account, provider);
}

/** An action's label as the verb in "⌘↵ to …", such as "start a review". */
function sentenceVerb(label: string): string {
  return label.charAt(0).toLowerCase() + label.slice(1);
}

export function PullRequestCommentForm({
  draft,
  setBody,
  post,
  account,
  provider = GITHUB_COPY,
  inline = false,
  newThread = false,
  disabled = false,
  markdown,
  paletteActive = false,
  stateAction,
  boxName,
  postBlocked = false,
  writesNotice,
  errorShownElsewhere = false,
  pendingReview = null,
  local = false,
  destinationControl,
  onCancel,
}: {
  draft: PullRequestCommentDraft;
  setBody: (body: string) => void;
  post: () => void;
  account: string | null;
  /** The PR's provider, which names the account a post goes out as. */
  provider?: CommentWritesProvider;
  inline?: boolean;
  newThread?: boolean;
  disabled?: boolean;
  /** Edits the draft in the Markdown composer on the new pull request page. */
  markdown?: CommentMarkdownBinding;
  /** Offer the folding box's ⌘K rows; only the shown panel's box does. */
  paletteActive?: boolean;
  /** The bottom box's Close / Reopen Pull Request, with its comment posted first. */
  stateAction?: PullRequestStateAction;
  /** The draft's name, by which Quote Reply asks this box to open. */
  boxName?: string;
  /** Posting can't work here now; the draft stays editable and `writesNotice` says why. */
  postBlocked?: boolean;
  writesNotice?: ReactNode;
  /** `writesNotice` already says what the draft's error does. */
  errorShownElsewhere?: boolean;
  /** A review conversation's box while pending reviews are on: what it offers instead. */
  pendingReview?: PendingComposer | null;
  /** The draft goes to the agent as a local comment, so it has no GitHub author or route. */
  local?: boolean;
  /** Chooses where the draft goes, shown beside its submit button. */
  destinationControl?: ReactNode;
  /** Closes a new conversation's box, as its Cancel button does. */
  onCancel?: () => void;
}) {
  const [tab, setTab] = useState("write");
  const formRef = useRef<HTMLFormElement>(null);
  const compactReply = inline && !newThread;
  // The new page edits comments in the Markdown composer; the classic page keeps the textarea.
  const pageV2 = usePullRequestImagePage() !== null;
  const composing = pageV2 && !!markdown;
  // The new page's own comment box folds to one line, as review replies do everywhere.
  const foldingBox = composing && !inline;
  const folds = compactReply || foldingBox;
  const [expanded, setExpanded] = useState(() => opensExpanded(draft));
  // Each draft (another workspace user's, or the next one after a post) opens on its own facts.
  const identity = markdown?.identity ?? "";
  const [expandedFor, setExpandedFor] = useState(identity);
  // Bumped when someone opens the box, so the textarea takes the caret; a draft that opens
  // the box by itself leaves focus where it is.
  const [focusRequest, setFocusRequest] = useState(0);
  if (identity !== expandedFor) {
    setExpandedFor(identity);
    setExpanded(opensExpanded(draft));
    // An open request belongs to the draft it was made for; the next draft's composer
    // mounts fresh and must not take the caret for it.
    setFocusRequest(0);
  }
  const rowRef = useRef<HTMLButtonElement>(null);
  const focusRowOnFold = useRef(false);
  const wasPending = useRef(draft.pending);
  useEffect(() => {
    if (folds && foldsAfterPost(wasPending.current, draft)) {
      setExpanded(false);
      setTab("write");
    }
    wasPending.current = draft.pending;
  }, [folds, draft]);
  const collapsed = folds && !expanded && !draft.pending;
  useEffect(() => {
    if (!collapsed || !focusRowOnFold.current) return;
    focusRowOnFold.current = false;
    rowRef.current?.focus();
  }, [collapsed]);
  // The page hides the box with CSS while the description is being edited; ⌘K must know.
  const [displayed, setDisplayed] = useState(true);
  useEffect(() => {
    const form = formRef.current;
    if (!foldingBox || !form || typeof ResizeObserver === "undefined") return;
    const update = () => setDisplayed(form.checkVisibility?.() ?? true);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(form);
    return () => observer.disconnect();
  }, [foldingBox]);
  const paletteRowsOwner = useId();
  const tooLong = pullRequestCommentTooLong(draft.body);
  const attachmentsAvailability = usePullRequestAttachmentsAvailability();
  const uploading = (draft.uploading ?? 0) > 0;
  const hasText = !!draft.body.trim();
  const label = newThread
    ? "New review comment"
    : inline
      ? "Reply to review conversation"
      : "New PR comment";
  const attribution = commentAttribution(account, provider);
  const attributionId = useId();
  const expand = () => {
    setExpanded(true);
    setFocusRequest((request) => request + 1);
  };
  // Quote Reply: open on Write with the caret after the quote. A copy the page hides stays put.
  useCommentBoxOpenRequests(boxName, () => {
    if (formRef.current?.checkVisibility?.() === false) return;
    setTab("write");
    expand();
  });
  // The classic box's textarea takes the caret, at the end, on each open request, once Write
  // shows it: switching back from Preview mounts it a moment after the request. A request
  // belongs to its draft: another draft's box starts with none, and never takes the caret for it.
  const servedFocusRequest = useRef({ identity, request: 0 });
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const serveFocusRequest = useCallback(() => {
    const textarea = textareaRef.current;
    const served = servedFocusRequest.current;
    if (!textarea || focusRequest === 0) return;
    if (served.identity === identity && served.request === focusRequest) return;
    servedFocusRequest.current = { identity, request: focusRequest };
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    textarea.scrollIntoView?.({ block: "nearest" });
  }, [focusRequest, identity]);
  useEffect(serveFocusRequest, [serveFocusRequest]);
  const attachTextarea = useCallback(
    (textarea: HTMLTextAreaElement | null) => {
      textareaRef.current = textarea;
      serveFocusRequest();
    },
    [serveFocusRequest],
  );
  /** Fold the form and keep its draft; a post under way stays open, so a failure shows. */
  // A close or reopen with this PR's comment is under way, from this box or another.
  const chainRunning = !!stateAction?.running;
  // Close and reopen also wait while an earlier one's outcome is unknown.
  const stateHeld = !!stateAction?.held;
  const offer = foldingBox
    ? (stateAction?.offer ?? { kind: "none" as const })
    : { kind: "none" as const };
  // A comment that can't be posted never rides along with Close or Reopen.
  const withComment = expanded && hasText && !postBlocked;
  const stateLabel =
    offer.kind !== "action"
      ? ""
      : chainRunning
        ? offer.operation === "pull_request_close"
          ? "Closing…"
          : "Reopening…"
        : withComment
          ? offer.operation === "pull_request_close"
            ? "Close with Comment"
            : "Reopen with Comment"
          : offer.label;
  const runStateChange = () => {
    if (!stateAction || disabled || stateHeld || draft.pending || uploading) return;
    if (withComment && tooLong) return;
    stateAction.run(withComment);
  };
  const collapse = () => {
    if (draft.pending || chainRunning) return;
    setExpanded(false);
    setTab("write");
    if (foldingBox) focusRowOnFold.current = true;
    else formRef.current?.focus();
  };
  // A reply folds and keeps its draft; a new conversation's box closes, as its Cancel does.
  const cancelAction = folds ? collapse : onCancel;
  const hasCancel = !!cancelAction;
  const cancel = cancelAction
    ? () => {
        if (!draft.pending) cancelAction();
      }
    : undefined;
  // The rows run whatever the box offers when chosen, never a callback from an older render.
  const latest = useRef({ expand, collapse, runStateChange, cancel });
  latest.current = { expand, collapse, runStateChange, cancel };
  // Of the open comment boxes, the one worked in last offers ⌘K's Cancel Comment; before any
  // is, the page's own box does.
  const focus = commentBoxes.useLastFocused();
  const { release: releaseFocus } = focus;
  useEffect(() => {
    if (collapsed) releaseFocus();
  }, [collapsed, releaseFocus]);
  const offersCancel =
    !collapsed && !draft.pending && hasCancel && (focus.active || (focus.none && foldingBox));
  const cancelRows = useMemo<PaletteCommand[]>(
    () =>
      offersCancel
        ? [
            {
              id: `pull-request-comment-box:${paletteRowsOwner}:cancel`,
              label: "Cancel Comment",
              icon: XIcon,
              keywords: ["close", "fold", "hide", "comment", "review"],
              // A box that folds focuses its row; a new conversation's box goes away.
              movesFocus: folds,
              run: () => latest.current.cancel?.(),
            },
          ]
        : NO_PALETTE_COMMANDS,
    [offersCancel, folds, paletteRowsOwner],
  );
  const offerOperation = offer.kind === "action" ? offer.operation : null;
  // A row belongs to one draft (one PR and workspace user); another's never runs it.
  const rowOwner = markdown?.identity ?? "";
  const stateRunnable =
    offer.kind === "action" &&
    !draft.pending &&
    !uploading &&
    !stateHeld &&
    !(withComment && tooLong);
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    // A box the page hides (while the description is edited) offers nothing to run.
    if (!foldingBox || !paletteActive || disabled || !displayed || chainRunning)
      return NO_PALETTE_COMMANDS;
    const rows: PaletteCommand[] = [
      {
        id: `pull-request-comment-box:${rowOwner}:add`,
        label: "Add a Comment",
        icon: MessageSquarePlusIcon,
        keywords: ["comment", "reply", "conversation", "pull request", "write"],
        movesFocus: true,
        run: () => latest.current.expand(),
      },
    ];
    rows.push(...cancelRows);
    // Closing or reopening alone is the page's own row, on every tab; the box adds its comment.
    if (offerOperation && stateRunnable && withComment)
      rows.push({
        id: `pull-request-comment-box:${rowOwner}:${offerOperation}:comment`,
        label: stateLabel,
        icon:
          offerOperation === "pull_request_close"
            ? GitPullRequestClosedIcon
            : GitPullRequestArrowIcon,
        keywords: [
          "pull request",
          "state",
          offerOperation === "pull_request_close" ? "close" : "reopen",
        ],
        run: () => latest.current.runStateChange(),
      });
    return rows;
  }, [
    foldingBox,
    paletteActive,
    disabled,
    cancelRows,
    displayed,
    chainRunning,
    offerOperation,
    stateLabel,
    withComment,
    stateRunnable,
    rowOwner,
  ]);
  usePaletteCommands(
    `pull-request-comment-box:${paletteRowsOwner}`,
    "Comment",
    foldingBox ? paletteCommands : cancelRows,
  );
  /** Post through every guard: nothing waiting and no upload still running. */
  const submit = () => {
    if (disabled || postBlocked || collapsed || draft.pending || uploading || chainRunning) return;
    // ⌘↵ and the button do what the box's first action says, through its own guards.
    if (pendingReview) {
      if (!pendingReview.primary.disabled) pendingReview.primary.run();
      return;
    }
    post();
  };
  return (
    <form
      ref={formRef}
      // Where focus goes once the last comment it could answer is deleted.
      data-comment-reply-row={inline ? "thread" : "conversation"}
      tabIndex={compactReply ? -1 : undefined}
      className={
        foldingBox
          ? "pull-request-comment-surface flex max-h-[60%] min-w-0 shrink-0 flex-col gap-2 px-4 pt-2 pb-3"
          : collapsed
            ? "pull-request-comment-surface min-w-0 border-t p-2"
            : inline
              ? "pull-request-comment-surface flex max-h-96 min-w-0 flex-col gap-2 border-t p-3"
              : "pull-request-comment-surface flex max-h-[60%] shrink-0 flex-col gap-2 border-t p-3"
      }
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      onFocus={hasCancel ? focus.markFocused : undefined}
    >
      {collapsed && foldingBox ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              ref={rowRef}
              type="button"
              variant="ghost"
              aria-expanded={false}
              aria-describedby={attributionId}
              onClick={expand}
              className="h-[34px] w-full min-w-0 cursor-text justify-start gap-[7px] rounded-[7px] border border-border/80 bg-background px-2.5 font-normal text-muted-foreground hover:border-ring/60 hover:bg-background hover:text-foreground focus-visible:border-ring/60 dark:bg-background dark:hover:bg-background"
            >
              <MessageSquarePlusIcon aria-hidden className="size-[13px] shrink-0" />
              <span className="min-w-0 truncate">{foldedSummary(draft, "Add a comment…")}</span>
            </Button>
          </TooltipTrigger>
          <TooltipContent>{attribution}</TooltipContent>
        </Tooltip>
      ) : collapsed ? (
        <Input
          aria-label="Reply to review conversation"
          aria-expanded={false}
          placeholder="Write a reply…"
          value={draft.body}
          onChange={(event) => setBody(event.target.value)}
          onFocus={() => setExpanded(true)}
          className="bg-transparent focus-visible:border-input focus-visible:outline-none focus-visible:ring-0 dark:bg-transparent"
        />
      ) : composing && markdown ? (
        <MarkdownComposer
          // Another workspace user's draft, or one sent or discarded since, gets a fresh composer.
          key={markdown.identity}
          value={draft.body}
          onChange={setBody}
          onSubmit={submit}
          onCancel={cancel}
          submitLabel={pendingReview?.primary.label ?? (compactReply ? "Reply" : "Post Comment")}
          submitVerb={
            pendingReview
              ? sentenceVerb(pendingReview.primary.label)
              : compactReply
                ? "reply"
                : "comment"
          }
          size="comment"
          label={label}
          placeholder={
            compactReply
              ? "Write a reply…"
              : foldingBox
                ? "Add to the conversation"
                : "Leave a comment…"
          }
          disabled={disabled || draft.pending || chainRunning}
          autoFocus={newThread || compactReply}
          focusRequest={focusRequest}
          preview={<PullRequestMarkdownPreview body={draft.body} />}
          attachments={
            attachmentsAvailability.available
              ? {
                  conversationId: markdown.conversationId,
                  prUrl: markdown.prUrl,
                  rememberUploadedImage,
                  updateDraftText: (change) =>
                    markdown.editDraft(
                      (current) => ({ body: change(current.body) }),
                      markdown.generation,
                    ),
                  onUploadingChange: (delta) =>
                    markdown.editDraft(
                      (current) => ({ uploading: (current.uploading ?? 0) + delta }),
                      markdown.generation,
                    ),
                  // Cleared before a new batch is checked, so its own refusals stay shown.
                  onAttachmentBatch: () =>
                    markdown.editDraft(() => ({ attachmentError: undefined }), markdown.generation),
                  onAttachmentError: (attachmentError) =>
                    markdown.editDraft(() => ({ attachmentError }), markdown.generation),
                }
              : null
          }
          attachmentsUnavailableReason={
            attachmentsAvailability.needsHost
              ? "Attaching files needs the session’s machine"
              : undefined
          }
          footerActions={
            foldingBox && (
              <>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={draft.pending || chainRunning}
                  onClick={collapse}
                >
                  Cancel
                </Button>
                {offer.kind === "action" && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={disabled || draft.pending || uploading || stateHeld}
                    title={
                      stateAction?.notice?.outcome.kind === "unknown"
                        ? "Check the pull request before changing it again."
                        : undefined
                    }
                    onClick={runStateChange}
                  >
                    {offer.operation === "pull_request_close" ? (
                      <GitPullRequestClosedIcon aria-hidden className="text-destructive" />
                    ) : (
                      <GitPullRequestArrowIcon aria-hidden className="text-success" />
                    )}
                    {stateLabel}
                  </Button>
                )}
                {offer.kind === "link" && (
                  <Button asChild variant="ghost" size="sm" className="text-muted-foreground">
                    <a href={offer.url} target="_blank" rel="noreferrer">
                      {offer.label}
                    </a>
                  </Button>
                )}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      type="submit"
                      size="sm"
                      aria-describedby={attributionId}
                      disabled={
                        disabled ||
                        postBlocked ||
                        draft.pending ||
                        uploading ||
                        !hasText ||
                        tooLong ||
                        chainRunning
                      }
                    >
                      {draft.pending ? "Posting…" : "Comment"}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{attribution}</TooltipContent>
                </Tooltip>
              </>
            )
          }
        />
      ) : (
        <Tabs value={tab} onValueChange={setTab} className="min-h-0">
          <TabsList className="shrink-0">
            <TabsTrigger value="write">Write</TabsTrigger>
            <TabsTrigger value="preview">Preview</TabsTrigger>
          </TabsList>
          <TabsContent value="write" className="min-h-0 overflow-y-auto">
            <Textarea
              ref={attachTextarea}
              aria-label={label}
              className="max-h-40 bg-transparent focus-visible:border-input focus-visible:outline-none focus-visible:ring-0 dark:bg-transparent"
              placeholder={inline && !newThread ? "Write a reply…" : "Leave a comment..."}
              autoFocus={newThread || compactReply}
              value={draft.body}
              disabled={draft.pending}
              onChange={(event) => setBody(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Escape" || !cancel || draft.pending) return;
                event.preventDefault();
                event.stopPropagation();
                cancel();
              }}
              rows={inline ? 2 : 4}
            />
          </TabsContent>
          <TabsContent value="preview" className="min-h-0 overflow-y-auto">
            {draft.body.trim() ? (
              <PullRequestMarkdownPreview body={draft.body} />
            ) : (
              <p className="text-muted-foreground">Nothing to preview.</p>
            )}
          </TabsContent>
        </Tabs>
      )}
      {(!collapsed || foldingBox) && (
        <>
          {foldingBox && (
            <span id={attributionId} className="sr-only">
              {attribution}
            </span>
          )}
          {tooLong && (
            <p role="alert">
              A comment can be at most {MAX_PULL_REQUEST_COMMENT_CHARACTERS} characters.
            </p>
          )}
          {draft.error && !errorShownElsewhere && (
            <p role="alert" className="text-destructive">
              {draft.error}
            </p>
          )}
          {writesNotice}
          {pendingReview?.notice && <PendingComposerNoticeRow notice={pendingReview.notice} />}
          {draft.attachmentError && markdown && (
            <div role="alert" className="flex flex-wrap items-center gap-x-2 text-destructive">
              <p className="min-w-0">{draft.attachmentError}</p>
              <Button
                type="button"
                variant="link"
                size="sm"
                className="h-auto p-0 text-muted-foreground"
                onClick={() =>
                  markdown.editDraft(() => ({ attachmentError: undefined }), markdown.generation)
                }
              >
                Dismiss
              </Button>
            </div>
          )}
        </>
      )}
      {!collapsed && !foldingBox && (
        // As on GitHub: who it's from on the left, then Cancel and the actions at the right,
        // the filled one last.
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {destinationControl}
          {!postBlocked && (
            <p
              className="min-w-0 truncate text-ui text-muted-foreground"
              title={account ?? undefined}
            >
              {commentAuthor(account, provider)}
            </p>
          )}
          <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-2">
            {cancel && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="shrink-0"
                disabled={draft.pending}
                onClick={cancel}
              >
                Cancel
              </Button>
            )}
            {pendingReview?.secondary && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="shrink-0"
                disabled={
                  disabled ||
                  postBlocked ||
                  draft.pending ||
                  uploading ||
                  !hasText ||
                  tooLong ||
                  pendingReview.secondary.disabled
                }
                onClick={pendingReview.secondary.run}
              >
                {pendingReview.secondary.label}
              </Button>
            )}
            <Button
              type="submit"
              size="sm"
              className="shrink-0"
              disabled={
                disabled ||
                postBlocked ||
                draft.pending ||
                uploading ||
                !hasText ||
                tooLong ||
                !!pendingReview?.primary.disabled
              }
            >
              {draft.pending
                ? pendingReview
                  ? "Sending…"
                  : local
                    ? "Saving…"
                    : "Posting…"
                : (pendingReview?.primary.label ??
                  (local
                    ? "Add comment"
                    : newThread
                      ? "Comment"
                      : inline
                        ? "Reply"
                        : "Post comment"))}
            </Button>
          </div>
        </div>
      )}
    </form>
  );
}

/** A pending review notice under the box, with its actions as links. */
function PendingComposerNoticeRow({ notice }: { notice: PendingComposerNotice }) {
  return (
    <div
      role={notice.tone === "alert" ? "alert" : "status"}
      className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui"
    >
      <p
        className={
          notice.tone === "alert" ? "min-w-0 text-destructive" : "min-w-0 text-muted-foreground"
        }
      >
        {notice.text}
      </p>
      {notice.actions.map((action) => (
        <Button
          key={action.label}
          type="button"
          variant="link"
          size="sm"
          className="h-auto p-0"
          disabled={action.disabled}
          onClick={action.run}
        >
          {action.label}
        </Button>
      ))}
    </div>
  );
}
