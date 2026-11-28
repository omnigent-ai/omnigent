// The new pull request page's Review ▾: a summary and a verdict (Comment, Approve or Request
// Changes), submitted on this Mac as its GitHub account. Where this Mac can't, ⌘K offers
// reviewing on GitHub instead. Its ⌘K rows come from the same definitions as the menu.

import type { PullRequestReviewer } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  type ComponentProps,
  forwardRef,
  type KeyboardEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { AlertTriangleIcon, BellRingIcon, ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { isMacPlatform } from "@/lib/hotkeys";
import { chordMatches, keyChordInput } from "@/lib/shortcuts/chord";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { openLink } from "@/lib/openLinkInApp";
import { cn } from "@/lib/utils";
import { MarkdownComposer } from "@/markdownEditor/MarkdownComposer";
import type { PullRequestDetailsState } from "@/pullRequestPage/detailsLayout";
import { useControlPlace } from "@/pullRequestPage/pullRequestDestination";
import {
  type ReviewerPingAction,
  type ReviewerPingPullRequest,
  reviewerPingSectionLabel,
  useReviewerPingActions,
} from "@/hooks/useReviewerPings";
import { PullRequestMarkdownPreview } from "@/newPullRequestPage/shell/PullRequestDetails";
import {
  isCommandAvailable,
  isCommandListed,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";

import { type ReviewCommandRow, reviewCommandRows } from "./reviewCommands";
import type { PendingFinish } from "./usePendingFinish";
import {
  REVIEW_VERDICTS,
  type ReviewEvent,
  requestReviewMenu,
  takeReviewMenuRequest,
  type TakenReviewMenuRequest,
  useReviewMenuRequestSerial,
  verdictUnavailableReason,
} from "./reviewSubmit";
import {
  type ReviewOffer,
  type ReviewPullRequest,
  pendingFinishUnavailableReason,
  submitUnavailableReason,
  type ReviewSubmitController,
  useReviewOffer,
  useReviewSubmit,
} from "./useReviewSubmit";

const short = (sha: string) => sha.slice(0, 7);

export function PullRequestReviewMenu({ state }: { state: PullRequestDetailsState }) {
  const pr: ReviewPullRequest = {
    url: state.pr.url,
    number: state.pr.number,
    state: state.pr.state,
    author: state.pr.author,
    // The head the page shows, which a review is pinned to.
    headSha: state.lifecycle.headSha,
    hostReviewState: hostReviewStateOf(state.pr.reviewers, state.hostAccount),
  };
  const offer = useReviewOffer(pr, state.copy.label, {
    conversationId: state.conversationId,
    account: state.hostAccount,
  });
  const pings = useListedReviewerPings(
    state.pr.reviewers
      ? {
          url: state.pr.url,
          number: state.pr.number,
          state: state.pr.state,
          author: state.pr.author,
          reviewers: state.pr.reviewers,
        }
      : null,
  );
  const openOnGitHub = (url: string) => openLink(state.conversationId, url);
  if (offer.kind === "host") {
    return (
      <HostReviewMenu
        conversationId={state.conversationId}
        pr={pr}
        offer={offer}
        active={state.active === true}
        openOnGitHub={openOnGitHub}
        pings={pings}
      />
    );
  }
  // Where reviews can't be submitted here, the header's button is there only for pings; the
  // way to GitHub stays in ⌘K, and joins the menu when it shows.
  const rows =
    offer.kind === "link"
      ? reviewCommandRows(offer, null, { openMenu: () => {}, openOnGitHub })
      : [];
  return (
    <>
      {rows.length > 0 && (
        <ReviewPaletteRows rows={rows} active={state.active === true} prUrl={pr.url} />
      )}
      {pings.length > 0 && <PingReviewMenu pings={pings} link={rows[0] ?? null} />}
    </>
  );
}

/** `account`'s latest review state among the PR's reviewers, for a host review's Check Again. */
function hostReviewStateOf(
  reviewers: readonly PullRequestReviewer[] | undefined,
  account: string | null | undefined,
): string | null {
  if (!account) return null;
  const own = reviewers?.find(
    (reviewer) =>
      reviewer.kind === "user" && reviewer.login.toLowerCase() === account.toLowerCase(),
  );
  return own?.state ?? null;
}

/** The reviewer pings Review ▾ lists for `pr`: runnable, or greyed out with a reason. */
function useListedReviewerPings(pr: ReviewerPingPullRequest | null): ReviewerPingAction[] {
  const actions = useReviewerPingActions(pr, "review_menu");
  return useMemo(
    () => [...actions.values()].filter(({ command }) => isCommandListed(command.availability)),
    [actions],
  );
}

/** Review ▾ where this Mac can't submit a review: pinging reviewers, and the way to GitHub. */
function PingReviewMenu({
  pings,
  link,
}: {
  pings: ReviewerPingAction[];
  link: ReviewCommandRow | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <ReviewTrigger />
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-[min(22rem,calc(100vw-2rem))] gap-2.5 p-3"
        aria-label="Review"
      >
        {link && (
          <Button
            variant="outline"
            size="sm"
            className="justify-start gap-1.5"
            onClick={() => {
              setOpen(false);
              link.run();
            }}
          >
            <link.icon aria-hidden className="size-3.5" />
            {link.paletteLabel}
          </Button>
        )}
        <ReviewerPingMenuSection pings={pings} onPick={() => setOpen(false)} divided={!!link} />
      </PopoverContent>
    </Popover>
  );
}

/** Review ▾'s reviewer pings, one per requested reviewer; hidden when there are none. */
function ReviewerPingMenuSection({
  pings: actions,
  onPick,
  divided = true,
}: {
  pings: ReviewerPingAction[];
  onPick: () => void;
  /** Ruled off from what's above it in the menu. */
  divided?: boolean;
}) {
  if (actions.length === 0) return null;
  const label = reviewerPingSectionLabel();
  return (
    <section
      aria-label={label}
      className={cn("flex flex-col gap-1", divided && "border-t border-border pt-2.5")}
    >
      <span className="text-ui font-semibold">{label}</span>
      {actions.map(({ command, run }) => {
        const available = isCommandAvailable(command.availability);
        const reason = command.availability.available ? undefined : command.availability.reason;
        return (
          <Button
            key={command.id}
            variant="ghost"
            size="sm"
            className="h-auto justify-start gap-1.5 px-1.5 py-1 text-left"
            disabled={!available}
            title={reason}
            onClick={() => {
              // The popover closes first, so the confirmation takes focus.
              onPick();
              run();
            }}
          >
            <BellRingIcon aria-hidden className="size-3.5 shrink-0" />
            <span className="flex min-w-0 flex-col">
              <span>{command.menuLabel}</span>
              {reason && <span className="text-muted-foreground">{reason}</span>}
            </span>
          </Button>
        );
      })}
    </section>
  );
}

const ReviewTrigger = forwardRef<
  HTMLButtonElement,
  ComponentProps<typeof Button> & { label?: string }
>(function ReviewTrigger({ label = "Review", ...props }, ref) {
  return (
    <Button ref={ref} variant="outline" size="sm" className="shrink-0 gap-1" {...props}>
      {label}
      <ChevronDownIcon aria-hidden className="size-3.5" />
    </Button>
  );
});

/** What Review ▾ reads as: a pending review to finish, or a change to it to check first. */
function triggerLabel(review: ReviewSubmitController): string {
  const { finish } = review;
  if (!finish) return "Review";
  if (finish.unresolved?.kind === "discarding") return "Discarding review…";
  const reviewHeld = !!finish.unresolved && finish.unresolved.kind !== "drafts";
  if (reviewHeld || review.entry.status.kind === "uncertain") return "Review unconfirmed";
  return finish.total === null ? "Finish review" : `Finish review · ${finish.total}`;
}

/** The menu's rows in ⌘K while the panel is the one shown. */
function ReviewPaletteRows({
  rows,
  active,
  prUrl,
}: {
  rows: ReviewCommandRow[];
  active: boolean;
  prUrl: string;
}): null {
  const rowsKey = rows
    .map((row) => `${row.id}:${row.paletteLabel}:${row.disabledReason ?? ""}`)
    .join("|");
  // Each run does what its row does now: the review, scope or head may have moved since.
  const latest = useRef(rows);
  latest.current = rows;
  const commands = useMemo<PaletteCommand[]>(
    () =>
      active
        ? rows.map((row) => ({
            id: `pull-request-review:${row.id}:${prUrl}`,
            label: row.paletteLabel,
            icon: row.icon,
            keywords: row.keywords,
            movesFocus: row.id !== "check",
            availability: row.disabledReason ? unavailableBecause(row.disabledReason) : undefined,
            run: () => latest.current.find((each) => each.id === row.id)?.run(),
          }))
        : [],
    // The rows are described by `rowsKey`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [active, prUrl, rowsKey],
  );
  const instance = useId();
  usePaletteCommands(`pull-request-review:${instance}`, "Pull request", commands);
  return null;
}

function HostReviewMenu({
  conversationId,
  pr,
  offer,
  active,
  openOnGitHub,
  pings,
}: {
  conversationId: string;
  pr: ReviewPullRequest;
  offer: ReviewOffer;
  active: boolean;
  openOnGitHub: (url: string) => void;
  pings: ReviewerPingAction[];
}) {
  const [open, setOpen] = useState(false);
  // The row or ⌘K request this menu opened for, until the person closes it or submits.
  const heldRequest = useRef<TakenReviewMenuRequest | null>(null);
  const review = useReviewSubmit({
    conversationId,
    pr,
    offer,
    open,
    onSubmitted: () => {
      heldRequest.current = null;
      setOpen(false);
    },
  });
  const openMenu = (event: ReviewEvent | null) => {
    if (event) review.setEvent(event);
    setOpen(true);
  };
  // ⌘K and list rows ask for the menu through the store, as they run outside this tree. The
  // shown menu takes a request, including one made just before this pull request opened.
  const requestSerial = useReviewMenuRequestSerial();
  // Only a request aimed at this view opens this menu.
  const { destination, active: shown } = useControlPlace();
  // Taken once the page has loaded the head a review pins to. A header that's replaced while
  // the menu it opened is still up hands the request back, so its replacement opens instead.
  const ready = active && shown && !!pr.headSha;
  useEffect(() => {
    if (!ready) return;
    const request = takeReviewMenuRequest(pr.url, Date.now(), { destination });
    if (!request) return;
    heldRequest.current = request;
    openMenu(request.event);
    // Only a new request, or this menu showing, opens it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestSerial, ready, pr.url, destination]);
  useEffect(
    () => () => {
      heldRequest.current?.release();
      heldRequest.current = null;
    },
    [],
  );
  const rows = reviewCommandRows(offer, review, {
    // Asked through the store, so whichever copy of this menu shows the PR opens.
    openMenu: (event) => requestReviewMenu(pr.url, event, { destination }),
    openOnGitHub,
  });
  return (
    <>
      <ReviewPaletteRows rows={rows} active={active} prUrl={pr.url} />
      <Popover
        open={open}
        onOpenChange={(next) => {
          // Seen and closed: the request it opened for is done.
          if (!next) heldRequest.current = null;
          setOpen(next);
        }}
      >
        <PopoverTrigger asChild>
          <ReviewTrigger label={triggerLabel(review)} />
        </PopoverTrigger>
        <PopoverContent
          align="end"
          className="max-h-(--radix-popover-content-available-height) w-[min(26rem,calc(100vw-2rem))] gap-3 overflow-y-auto p-3"
          aria-label={review.finish ? "Finish your review" : "Submit review"}
        >
          <ReviewSubmitForm review={review} prUrl={pr.url} openOnGitHub={openOnGitHub} />
          <ReviewerPingMenuSection pings={pings} onPick={() => setOpen(false)} />
        </PopoverContent>
      </Popover>
      {review.finish && <DiscardPendingReviewDialog finish={review.finish} />}
      <KeepAsDraftDialog keep={review.keepAsDraft} />
    </>
  );
}

/** Keep as Draft's confirmation: a review GitHub did make would be sent twice. */
function KeepAsDraftDialog({ keep }: { keep: ReviewSubmitController["keepAsDraft"] }) {
  return (
    <Dialog
      open={keep.asked}
      onOpenChange={(next) => {
        if (!next) keep.cancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Keep your review as a draft?</DialogTitle>
          <DialogDescription>
            Only do this if GitHub doesn’t show your review. Its summary and verdict come back to
            edit, and submitting it again makes a new review.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={keep.cancel}>
            Cancel
          </Button>
          <Button onClick={keep.confirm}>Keep as Draft</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Discard's confirmation: the review's comments are deleted for good, so it asks first. */
function DiscardPendingReviewDialog({ finish }: { finish: PendingFinish }) {
  const total = finish.total ?? 0;
  const partial = finish.shown !== null && finish.total !== null && finish.shown < finish.total;
  return (
    <Dialog
      open={finish.discard.asked}
      onOpenChange={(next) => {
        if (!next) finish.discard.cancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Discard your pending review?</DialogTitle>
          <DialogDescription>
            This deletes its {total} {total === 1 ? "comment" : "comments"}
            {partial ? `, including ${total - (finish.shown ?? 0)} not shown here` : ""}. They can’t
            be recovered.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={finish.discard.cancel}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={finish.discard.confirm}>
            Discard Review
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** What finishing says about the review: its count, and its commit when it's behind the head. */
function PendingFinishSummary({
  finish,
  prUrl,
  openOnGitHub,
}: {
  finish: PendingFinish;
  prUrl: string;
  openOnGitHub: (url: string) => void;
}) {
  const { total, shown, outdated } = finish;
  const partial = total !== null && shown !== null && shown < total;
  return (
    <>
      <div className="flex flex-col gap-0.5">
        <strong className="text-ui font-semibold">Finish your review</strong>
        <span className="text-ui text-muted-foreground">
          {total === null
            ? "Couldn’t count this review’s comments."
            : partial
              ? `Submitting posts all ${total} comments in this review. ${shown} are shown here; the rest are on GitHub.`
              : `Submitting posts your ${total} ${total === 1 ? "comment" : "comments"}.`}{" "}
          {(partial || total === null) && (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => openOnGitHub(`${prUrl}/files`)}
            >
              Open on GitHub
            </Button>
          )}
        </span>
      </div>
      {outdated && (
        <div
          role="status"
          className="flex flex-col gap-1 rounded-md bg-warning/10 px-2.5 py-1.5 text-ui"
        >
          <span>
            Review started on{" "}
            <code title={outdated.reviewCommit}>{short(outdated.reviewCommit)}</code>
            {outdated.newer === null
              ? ", before the latest changes."
              : `; ${outdated.newer} newer ${outdated.newer === 1 ? "commit" : "commits"}.`}
          </span>
          {finish.discardable && (
            <Button
              variant="link"
              size="sm"
              className="h-auto self-start p-0"
              disabled={!!finish.unresolved || finish.discard.sending}
              onClick={finish.discard.ask}
            >
              Discard and start on the latest commit
            </Button>
          )}
        </div>
      )}
      {finish.conflict && (
        <div role="status" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui">
          <span className="min-w-0">The summary on GitHub changed.</span>
          <Button variant="link" size="sm" className="h-auto p-0" onClick={finish.takeGitHubs}>
            Use GitHub’s
          </Button>
          <Button variant="link" size="sm" className="h-auto p-0" onClick={finish.keepMine}>
            Keep Mine
          </Button>
        </div>
      )}
      {finish.unresolved?.kind === "discard" && (
        <div
          role="status"
          className="flex flex-col gap-1.5 rounded-md bg-warning/10 px-2.5 py-2 text-ui"
        >
          <span className="flex items-start gap-1.5">
            <AlertTriangleIcon aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
            <span>
              Couldn’t confirm whether GitHub discarded your review.
              {finish.unresolved.hold.checked === "unreadable" &&
                " The app couldn’t check GitHub. Try again."}
              {finish.unresolved.hold.checked === "unsettled" &&
                " GitHub doesn’t show it gone yet."}
            </span>
          </span>
          <span className="flex gap-3">
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              disabled={finish.unresolved.hold.checking}
              onClick={finish.checkDiscard}
            >
              {finish.unresolved.hold.checking ? "Checking…" : "Check Again"}
            </Button>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => openOnGitHub(prUrl)}
            >
              Open on GitHub
            </Button>
          </span>
        </div>
      )}
      {finish.notice && (
        <p role="status" className="text-ui text-muted-foreground">
          {finish.notice}
        </p>
      )}
    </>
  );
}

function ReviewSubmitForm({
  review,
  prUrl,
  openOnGitHub,
}: {
  review: ReviewSubmitController;
  prUrl: string;
  openOnGitHub: (url: string) => void;
}) {
  const { entry, headSha, viewer, author, finish } = review;
  const { status } = entry;
  const reason = viewer
    ? finish
      ? pendingFinishUnavailableReason(entry, finish, { viewer, author, headSha })
      : submitUnavailableReason(entry, { viewer, author, headSha, pending: false })
    : null;
  const locked = status.kind === "submitting" || status.kind === "uncertain";
  const summaryId = useId();
  // ⌘↵ submits from anywhere in the form, as Submit Review would: a verdict, a button, the
  // summary's tabs and formatting buttons. Where the summary's editor submits on ⌘↵ itself (its
  // text and its preview), it marks the key handled, and the form leaves it. Only there: a radio
  // marks every Enter handled too, and must still submit.
  const submitFromKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.repeat) return;
    const inSummary =
      event.target instanceof Element && event.target.closest("[data-review-summary]") !== null;
    if (inSummary && event.defaultPrevented) return;
    if (!chordMatches(SUBMIT_CHORD, keyChordInput(event.nativeEvent), isMacPlatform())) return;
    event.preventDefault();
    if (reason === null) review.submit();
  };
  return (
    // Its own box only for the keys: the form's parts stay the popover's flex children.
    <div className="contents" onKeyDown={submitFromKeys}>
      {finish ? (
        <PendingFinishSummary finish={finish} prUrl={prUrl} openOnGitHub={openOnGitHub} />
      ) : (
        <div className="flex flex-col gap-0.5">
          <strong className="text-ui font-semibold">Review changes</strong>
          <span className="text-ui text-muted-foreground">
            Write a summary, then choose how to submit it.
          </span>
        </div>
      )}
      {headSha ? (
        <p className="text-ui text-muted-foreground">
          Reviewing <code title={headSha}>{short(headSha)}</code>.
        </p>
      ) : null}
      <div data-review-summary="" className="contents">
        <MarkdownComposer
          // One editor per account's review, so another account's text never shows in it.
          key={review.key ?? "none"}
          value={review.body}
          onChange={review.setBody}
          onSubmit={review.submit}
          submitLabel="Submit Review"
          submitVerb="submit"
          size="comment"
          label="Review summary"
          placeholder={
            entry.event === "REQUEST_CHANGES"
              ? "Explain what needs to change"
              : entry.event === "APPROVE"
                ? "Leave a summary (optional)"
                : "Leave a summary"
          }
          disabled={locked}
          autoFocus
          preview={<PullRequestMarkdownPreview body={review.body} />}
          attachments={null}
        />
      </div>
      <RadioGroup
        aria-label="Review verdict"
        value={entry.event}
        onValueChange={(value) => review.setEvent(value as ReviewEvent)}
        disabled={locked}
        className="gap-2.5"
      >
        {REVIEW_VERDICTS.filter((verdict) => review.events.includes(verdict.event)).map(
          (verdict) => {
            const unavailable = verdictUnavailableReason(verdict.event, viewer, author);
            const id = `${summaryId}-${verdict.event}`;
            return (
              <label
                key={verdict.event}
                htmlFor={id}
                className={
                  unavailable
                    ? "flex cursor-not-allowed items-start gap-2 text-muted-foreground"
                    : "flex cursor-pointer items-start gap-2"
                }
              >
                <RadioGroupItem
                  id={id}
                  value={verdict.event}
                  disabled={!!unavailable || locked}
                  className="mt-0.5"
                />
                <span className="flex flex-col">
                  <span className="text-ui font-medium">{verdict.title}</span>
                  <span className="text-ui text-muted-foreground">
                    {unavailable ?? verdict.detail}
                    {/* An older review's approval or request isn't about the newest commits. */}
                    {!unavailable && finish?.outdated && verdict.event !== "COMMENT" && (
                      <> This doesn’t review {short(finish.outdated.head)}.</>
                    )}
                  </span>
                </span>
              </label>
            );
          },
        )}
      </RadioGroup>
      {status.kind === "refused" && (
        <p role="alert" className="text-ui text-destructive">
          {status.message}
        </p>
      )}
      {status.kind === "uncertain" && (
        <div
          role="status"
          className="flex flex-col gap-1.5 rounded-md bg-warning/10 px-2.5 py-2 text-ui"
        >
          <span className="flex items-start gap-1.5">
            <AlertTriangleIcon aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" />
            <span>
              {status.message}
              {status.checked === "not_found" &&
                " Your review doesn’t show on GitHub yet; it may not have been submitted."}
              {status.checked === "unreadable" && " The app couldn’t check GitHub. Try again."}
              {status.checked === "ambiguous" &&
                " GitHub shows more than one review like this one, so the app can’t tell which is yours. Open the pull request on GitHub to see them."}
              {status.checked === "unidentified" &&
                " GitHub shows your review state, but not which review it is, so the app can’t confirm this one. Open the pull request on GitHub to check."}
            </span>
          </span>
          <span className="flex gap-3">
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              disabled={status.checking}
              onClick={review.checkAgain}
            >
              {status.checking ? "Checking…" : "Check Again"}
            </Button>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0"
              onClick={() => openOnGitHub(prUrl)}
            >
              Open on GitHub
            </Button>
            {review.keepAsDraft.available && (
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0"
                onClick={review.keepAsDraft.ask}
              >
                Keep as Draft…
              </Button>
            )}
          </span>
        </div>
      )}
      <div className="flex items-center justify-end gap-3">
        {/* A disabled button shows no tooltip, so say why beside it; an empty summary is plain. */}
        {reason && status.kind === "idle" && review.body.trim() !== "" && (
          <span className="min-w-0 flex-1 text-ui text-muted-foreground">{reason}</span>
        )}
        {finish?.discardable && (
          <Button
            size="sm"
            variant="outline"
            disabled={
              !!finish.unresolved || finish.discard.sending || locked || finish.total === null
            }
            onClick={finish.discard.ask}
          >
            Discard Review…
          </Button>
        )}
        <Button size="sm" disabled={reason !== null} onClick={review.submit}>
          {status.kind === "submitting" ? "Submitting…" : "Submit Review"}
        </Button>
      </div>
    </div>
  );
}

/** The chord that submits the review, as the summary editor offers it. */
const SUBMIT_CHORD = { mod: true, key: "Enter" } as const;
