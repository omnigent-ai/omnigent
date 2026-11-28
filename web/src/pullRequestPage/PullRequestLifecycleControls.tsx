// The new page's homes for Convert to draft, Ready for review, Enable auto-merge, Close and
// Reopen: the state pill's menu, "Still in progress? Convert to draft" under the checks, the
// merge bar's button, the notice of what a change came to, and the dialogs and ⌘K rows. Each
// renders from `usePullRequestLifecycleActions` or, for Close and Reopen, the comment box's own
// `usePullRequestStateAction`, so labels, availability and behavior can't drift.

import { type ReactNode, useEffect, useId, useMemo, useRef } from "react";
import {
  CircleAlertIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  XIcon,
  GitMergeIcon,
  GitPullRequestArrowIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  RefreshCwIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import {
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";

import {
  LIFECYCLE_ACTION_IDS,
  LIFECYCLE_ACTIONS,
  LIFECYCLE_HELD_REASON,
  type LifecycleActionId,
  type LifecycleNoticeAction,
  lifecycleNoticeActionLabel,
  lifecycleNoticeActions,
  mergeableNowNotice,
  updateLifecycleActionState,
  MERGEABLE_NOW_MESSAGE,
} from "./pullRequestLifecycleActions";
import { EnableAutoMergeDialog } from "./EnableAutoMergeDialog";
import { LifecycleNoticeView } from "./LifecycleNoticeView";
import { MERGE_ON_GITHUB, mergeOnGitHub, mergeOnGitHubDescription } from "./nativeStackMerge";
import {
  confirmationApplies,
  type LifecyclePullRequest,
  usePullRequestLifecycleActions,
} from "./usePullRequestLifecycleActions";
import { stateNoticeActionIcon, StateChainNoticeView } from "./StateChainNoticeView";
import {
  stateNoticeActionLabel,
  stateNoticeActions,
  type StateNoticeAction,
} from "./pullRequestStateChain";
import type { StateChangeOperation } from "@/lib/pullRequestStateHolds";
import { STATE_CHANGES } from "./pullRequestStateChanges";
import {
  type PullRequestStateAction,
  usePullRequestStateAction,
} from "./usePullRequestStateAction";
import { statePillProgress, type StatePillProgress } from "./statePillProgress";
import { focusOnOpen } from "./dialogFocus";

const PILL_ACTIONS: LifecycleActionId[] = ["convert_to_draft", "ready_for_review"];
const NOTICE_ICONS = {
  check: RefreshCwIcon,
  open: ExternalLinkIcon,
  dismiss: XIcon,
} as const;
const PALETTE_ICONS = {
  convert_to_draft: GitPullRequestDraftIcon,
  ready_for_review: GitPullRequestDraftIcon,
  enable_auto_merge: GitMergeIcon,
  disable_auto_merge: GitMergeIcon,
} as const;

/** A notice's row under the page header, spaced as the header's other notices. */
const HEADER_NOTICE_ROW = "px-4 py-1.5";

/** Close and Reopen alone, with no comment: the box's own change, started from elsewhere. */
const NO_COMMENT = () => Promise.resolve(false);

/** Close or Reopen for `pr`, sharing the comment box's running change, holds and notice. */
function usePullRequestStateChange(pr: LifecyclePullRequest): PullRequestStateAction {
  return usePullRequestStateAction({
    conversationId: pr.conversationId,
    prUrl: pr.prUrl,
    prState: pr.state,
    post: NO_COMMENT,
  });
}

/** Run a Close/Reopen notice's action, from its button or its ⌘K row. */
function runStateNoticeAction(change: PullRequestStateAction, action: StateNoticeAction) {
  if (action === "retry") change.retry();
  else if (action === "check") void change.checkAgain();
  else if (action === "dismiss") change.dismiss();
}

/** What the state pill says of the change to `pr` under way or just failed; see the menu. */
export function usePullRequestStatePillProgress(pr: LifecyclePullRequest): StatePillProgress {
  const actions = usePullRequestLifecycleActions(pr);
  const stateChange = usePullRequestStateChange(pr);
  return statePillProgress({
    stateRunning: stateChange.runningOperation,
    lifecycleRunning: actions.state.running,
    stateNotice: stateChange.notice,
    lifecycleNotice: actions.state.notice,
  });
}

/**
 * The state pill, opening a menu of the state changes that apply; a plain pill otherwise. While
 * one of them is on its way the badge shimmers (its owner asks
 * {@link usePullRequestStatePillProgress}), and one that failed leaves an alert beside it; the
 * notice under the header says why and offers Try Again.
 */
export function PullRequestStateMenu({
  pr,
  badge,
  currentLabel,
}: {
  pr: LifecyclePullRequest;
  badge: ReactNode;
  /** The state the badge shows, said by the menu's button too, whose name is its own. */
  currentLabel?: string;
}) {
  const actions = usePullRequestLifecycleActions(pr);
  const stateChange = usePullRequestStateChange(pr);
  const items = PILL_ACTIONS.filter((id) => actions.availability[id].kind !== "hidden");
  const { offer } = stateChange;
  const progress = usePullRequestStatePillProgress(pr);
  const pending = progress.kind === "running";
  if (items.length === 0 && offer.kind === "none" && progress.kind === "idle") return badge;
  const said = progress.kind === "idle" ? currentLabel : progress.label;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Pull request state"
          aria-description={said}
          aria-busy={pending || undefined}
          title={said}
          className="inline-flex shrink-0 items-center gap-0.5 rounded-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          {badge}
          {progress.kind === "failed" && (
            <CircleAlertIcon
              aria-hidden
              data-testid="state-pill-failed"
              className="size-3.5 text-destructive"
            />
          )}
          <ChevronDownIcon aria-hidden className="size-3 text-muted-foreground" />
        </button>
      </DropdownMenuTrigger>
      {/* Says the change is under way; the notice under the header says how it ended. */}
      <span role="status" className="sr-only">
        {pending ? progress.label : ""}
      </span>
      <DropdownMenuContent align="start">
        {items.map((id) => {
          const availability = actions.availability[id];
          const definition = LIFECYCLE_ACTIONS[id];
          if (availability.kind === "github") {
            return (
              <DropdownMenuItem key={id} asChild>
                <a href={availability.url} target="_blank" rel="noreferrer">
                  {definition.githubLabel}
                </a>
              </DropdownMenuItem>
            );
          }
          return (
            <DropdownMenuItem
              key={id}
              disabled={availability.kind === "held"}
              title={availability.kind === "held" ? availability.reason : undefined}
              onSelect={() => actions.request(id)}
            >
              {definition.label}
            </DropdownMenuItem>
          );
        })}
        {items.length > 0 && offer.kind !== "none" && <DropdownMenuSeparator />}
        {offer.kind === "link" && (
          <DropdownMenuItem asChild>
            <a href={offer.url} target="_blank" rel="noreferrer">
              {offer.label}
            </a>
          </DropdownMenuItem>
        )}
        {offer.kind === "action" && (
          <DropdownMenuItem
            disabled={stateChange.held}
            title={stateChange.heldReason ?? undefined}
            onSelect={() => stateChange.run(false)}
          >
            {STATE_CHANGES[offer.operation].menuLabel}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// The question each prompt under the checks asks before its action.
const PROMPT_LEADS = {
  convert_to_draft: "Still in progress?",
  ready_for_review: "Still a draft?",
} as const satisfies Partial<Record<LifecycleActionId, string>>;

type PromptActionId = keyof typeof PROMPT_LEADS;

/** Labels as a link reads them, without the menu's trailing "…". */
const promptLabel = (id: PromptActionId) => LIFECYCLE_ACTIONS[id].label.replace(/…$/, "");

/** A prompt under the checks for `action`, while it applies: GitHub's own place for it. */
function LifecyclePrompt({ pr, action }: { pr: LifecyclePullRequest; action: PromptActionId }) {
  const actions = usePullRequestLifecycleActions(pr);
  const availability = actions.availability[action];
  if (availability.kind === "hidden") return null;
  const definition = LIFECYCLE_ACTIONS[action];
  const linkClass = "text-foreground underline underline-offset-3 hover:text-link";
  return (
    <p className="text-right text-ui text-muted-foreground">
      {PROMPT_LEADS[action]}{" "}
      {availability.kind === "github" ? (
        <a href={availability.url} target="_blank" rel="noreferrer" className={linkClass}>
          {definition.githubLabel}
        </a>
      ) : (
        <button
          type="button"
          className={`${linkClass} disabled:cursor-not-allowed disabled:no-underline disabled:opacity-60`}
          disabled={availability.kind === "held"}
          title={availability.kind === "held" ? availability.reason : undefined}
          onClick={() => actions.request(action)}
        >
          {actions.state.running === action ? definition.running : promptLabel(action)}
        </button>
      )}
    </p>
  );
}

/** "Still in progress? Convert to draft" under the checks, for an open, ready PR. */
export function ConvertToDraftPrompt({ pr }: { pr: LifecyclePullRequest }) {
  return <LifecyclePrompt pr={pr} action="convert_to_draft" />;
}

/** "Still a draft? Ready for review" under the checks, for an open draft, which can't merge. */
export function ReadyForReviewPrompt({ pr }: { pr: LifecyclePullRequest }) {
  return <LifecyclePrompt pr={pr} action="ready_for_review" />;
}

/**
 * The merge bar's Enable auto-merge, before Merge: only where this Mac can turn it on, the base
 * has no queue and the PR waits on something. After GitHub says it can merge now, the bar says
 * so and Merge takes focus instead.
 */
export function EnableAutoMergeSlot({ pr }: { pr: LifecyclePullRequest }) {
  const actions = usePullRequestLifecycleActions(pr);
  const availability = actions.availability.enable_auto_merge;
  const notice = actions.state.notice;
  const mergeableNow = mergeableNowNotice(notice);
  const messageRef = useRef<HTMLParagraphElement>(null);
  // Merge takes focus once, as soon as it's enabled: the refusal usually lands before the read
  // that shows the PR can merge, so watch the bar until then, and never take focus again.
  useEffect(() => {
    const section = mergeableNow ? messageRef.current?.closest("section") : null;
    if (!section) return;
    const focusMerge = () => {
      const merge = section.querySelector<HTMLButtonElement>("[data-merge-button]:not(:disabled)");
      if (!merge) return false;
      merge.focus();
      return true;
    };
    if (focusMerge()) return;
    const observer = new MutationObserver(() => {
      if (focusMerge()) observer.disconnect();
    });
    observer.observe(section, { subtree: true, childList: true, attributes: true });
    return () => observer.disconnect();
  }, [mergeableNow]);
  return (
    <>
      {availability.kind === "mac" || availability.kind === "held" ? (
        <Button
          size="sm"
          variant="outline"
          disabled={availability.kind === "held"}
          title={availability.kind === "held" ? availability.reason : undefined}
          onClick={() => actions.request("enable_auto_merge")}
        >
          {actions.state.running === "enable_auto_merge" && <Spinner aria-hidden />}
          {LIFECYCLE_ACTIONS.enable_auto_merge.label}
        </Button>
      ) : availability.kind === "github" ? (
        <Button asChild size="sm" variant="ghost">
          <a href={availability.url} target="_blank" rel="noreferrer">
            {LIFECYCLE_ACTIONS.enable_auto_merge.githubLabel}
          </a>
        </Button>
      ) : null}
      {mergeableNow && notice && (
        <p
          ref={messageRef}
          role="status"
          className="order-last flex w-full items-center gap-2 text-ui text-muted-foreground"
        >
          <span className="min-w-0 flex-1">{MERGEABLE_NOW_MESSAGE}</span>
          <Button size="sm" variant="ghost" onClick={() => actions.runNoticeAction("dismiss")}>
            {lifecycleNoticeActionLabel(notice, "dismiss", false)}
          </Button>
        </p>
      )}
    </>
  );
}

/**
 * What the last change to `pr` came to, under the page's header on every tab, wherever it was
 * started: a Close or Reopen, then a draft or auto-merge change.
 */
export function PullRequestChangeNotice({ pr }: { pr: LifecyclePullRequest }) {
  const stateChange = usePullRequestStateChange(pr);
  const { notice } = stateChange;
  return (
    <>
      {notice && (
        <StateChainNoticeView
          notice={notice}
          prUrl={pr.prUrl}
          running={stateChange.running}
          checking={stateChange.checking}
          checkFailed={stateChange.checkFailed}
          onAction={(action) => runStateNoticeAction(stateChange, action)}
          className={HEADER_NOTICE_ROW}
        />
      )}
      <PullRequestLifecycleNotice pr={pr} />
    </>
  );
}

/** What the last draft or auto-merge change came to, kept per PR until dismissed or checked. */
export function PullRequestLifecycleNotice({ pr }: { pr: LifecyclePullRequest }) {
  const actions = usePullRequestLifecycleActions(pr);
  const notice = actions.state.notice;
  // The merge bar shows the one it owns.
  if (!notice || mergeableNowNotice(notice)) return null;
  return (
    <LifecycleNoticeView
      notice={notice}
      prUrl={pr.prUrl}
      checking={actions.state.checking}
      checkFailed={actions.state.checkFailed}
      onAction={actions.runNoticeAction}
      className={HEADER_NOTICE_ROW}
    />
  );
}

/**
 * Mounted once per page on every tab: the confirmations, wherever they were asked for, and the
 * ⌘K rows, while the page is `active`.
 */
export function PullRequestLifecycleOwner({
  pr,
  active,
}: {
  pr: LifecyclePullRequest;
  active: boolean;
}) {
  const { prUrl } = pr;
  const actions = usePullRequestLifecycleActions(pr);
  const stateChange = usePullRequestStateChange(pr);
  const instance = useId();
  // Rows call whatever the latest render offers: its head, method, account and owner.
  const latest = useRef({ actions, stateChange });
  useEffect(() => {
    latest.current = { actions, stateChange };
  });
  const runnable = LIFECYCLE_ACTION_IDS.filter((id) => {
    const kind = actions.availability[id].kind;
    return kind === "mac" || kind === "held";
  });
  const heldIds = runnable.filter((id) => actions.availability[id].kind === "held");
  // Everything the rows show, as values, so they're rebuilt exactly when one changes.
  const notice = actions.state.notice;
  const handoff = pr.mergeOnGitHub ?? null;
  // Close or Reopen alone; the comment box offers it with its comment.
  const stateOperation = stateChange.offer.kind === "action" ? stateChange.offer.operation : null;
  const stateNotice = stateChange.notice;
  const signature = JSON.stringify({
    key: actions.key,
    handoff,
    runnable,
    held: heldIds,
    notice: notice
      ? lifecycleNoticeActions(notice).map((action) => ({
          action,
          label: lifecycleNoticeActionLabel(notice, action, true),
        }))
      : [],
    state: stateOperation,
    stateHeld: stateChange.heldReason,
    stateNotice: stateNotice
      ? stateNoticeActions(stateNotice)
          .filter((action) => !(action === "retry" && stateChange.running))
          .map((action) => ({ action, label: stateNoticeActionLabel(stateNotice, action, true) }))
      : [],
  });
  const rows = useMemo<PaletteCommand[]>(() => {
    if (!active) return [];
    const shown = JSON.parse(signature) as {
      handoff: typeof handoff;
      runnable: LifecycleActionId[];
      held: LifecycleActionId[];
      notice: { action: LifecycleNoticeAction; label: string }[];
      state: StateChangeOperation | null;
      stateHeld: string | null;
      stateNotice: { action: StateNoticeAction; label: string }[];
    };
    const commands: PaletteCommand[] = [];
    // The merge bar's Merge on GitHub, for a PR GitHub merges with its stack.
    if (shown.handoff) {
      const merge = shown.handoff;
      commands.push({
        id: `pull-request-merge-on-github:${prUrl}`,
        label: MERGE_ON_GITHUB.paletteLabel,
        description: mergeOnGitHubDescription(merge),
        icon: ExternalLinkIcon,
        keywords: [...MERGE_ON_GITHUB.keywords],
        run: () => mergeOnGitHub(merge),
      });
    }
    for (const id of shown.runnable) {
      commands.push({
        id: `pull-request-lifecycle:${id}:${prUrl}`,
        label: LIFECYCLE_ACTIONS[id].paletteLabel,
        icon: PALETTE_ICONS[id],
        keywords: ["pull request", "draft", "ready", "review", "auto-merge", "automerge", "github"],
        ...(shown.held.includes(id)
          ? { availability: unavailableBecause(LIFECYCLE_HELD_REASON) }
          : {}),
        run: () => latest.current.actions.request(id),
      });
    }
    for (const { action, label } of shown.notice) {
      commands.push({
        id: `pull-request-lifecycle:notice:${action}:${prUrl}`,
        label,
        icon: NOTICE_ICONS[action],
        keywords: ["pull request", "notice", "check", "github"],
        run: () => latest.current.actions.runNoticeAction(action),
      });
    }
    if (shown.state) {
      const definition = STATE_CHANGES[shown.state];
      commands.push({
        id: `pull-request-state:${shown.state}:${prUrl}`,
        label: definition.label,
        icon:
          shown.state === "pull_request_close" ? GitPullRequestClosedIcon : GitPullRequestArrowIcon,
        keywords: [...definition.keywords],
        ...(shown.stateHeld ? { availability: unavailableBecause(shown.stateHeld) } : {}),
        run: () => latest.current.stateChange.run(false),
      });
    }
    for (const { action, label } of shown.stateNotice) {
      commands.push({
        id: `pull-request-state:notice:${action}:${prUrl}`,
        label,
        icon: stateNoticeActionIcon(action),
        keywords: ["pull request", "close", "reopen", "notice"],
        run: () =>
          action === "open"
            ? window.open(prUrl, "_blank", "noreferrer")
            : runStateNoticeAction(latest.current.stateChange, action),
      });
    }
    return commands;
  }, [active, prUrl, signature]);
  usePaletteCommands(`pull-request-lifecycle:${instance}`, "Pull request", rows);
  // Only this surface's own dialog, only while it's active, and only while its change still
  // applies: a hidden pane's leaves, as does one GitHub now merges with its stack.
  const owned =
    actions.state.confirming?.surface === actions.surface ? actions.state.confirming : null;
  const applies = !!owned && confirmationApplies(actions.availability[owned.id]);
  const confirming = active && applies ? owned : null;
  const retired = !!owned && (!active || !applies);
  useEffect(() => {
    if (retired) latest.current.actions.cancelConfirm();
  }, [retired]);
  // Leaving this PR, user or surface takes this owner's unsent confirmation with it; a change
  // already sent settles in the store all the same.
  const { key, surface } = actions;
  useEffect(
    () => () => {
      updateLifecycleActionState(key, (current) =>
        current.confirming?.surface === surface ? { ...current, confirming: null } : current,
      );
    },
    [key, surface],
  );
  return (
    <>
      <ConvertToDraftDialog
        open={confirming?.id === "convert_to_draft"}
        number={pr.number}
        onConfirm={() => actions.confirm()}
        onCancel={actions.cancelConfirm}
      />
      {confirming?.id === "enable_auto_merge" && (
        <EnableAutoMergeDialog
          number={pr.number}
          headSha={confirming.headSha ?? ""}
          methods={actions.methods}
          initialMethod={confirming.method ?? actions.defaultMethod}
          onConfirm={(method) => actions.confirm(method)}
          onCancel={actions.cancelConfirm}
        />
      )}
    </>
  );
}

/** Asks before converting `number` to a draft; shared by the PR page and the list's rows. */
export function ConvertToDraftDialog({
  open,
  number,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  number: number;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      {/* Cancel takes focus, so Enter alone never converts; Esc cancels. */}
      <DialogContent onOpenAutoFocus={focusOnOpen(cancelRef)}>
        <DialogHeader>
          <DialogTitle>Convert #{number} to a draft?</DialogTitle>
          <DialogDescription>
            Reviewers aren’t asked to review a draft, and it can’t merge. You can mark it ready for
            review again at any time.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button ref={cancelRef} variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={onConfirm}>Convert to Draft</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
