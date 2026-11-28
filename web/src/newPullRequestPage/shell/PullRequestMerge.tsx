// The Summary tab's Merge button: its state follows GitHub's mergeStateStatus and
// auto-merge, and a click opens a confirmation where the merge method can change. Where the
// base merges through a queue and the session's host serves it, the button asks to add it to the queue.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ExternalLinkIcon, GitMergeIcon } from "lucide-react";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import {
  useDisablePullRequestAutoMerge,
  useMergePullRequest,
  usePullRequestMergeOptions,
  type PullRequest,
  type PullRequestMergeMethod,
  useSetPullRequestPreference,
  type PullRequestAccount,
  type PullRequestAuth,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import type { MergeQueueState } from "@/lib/hostMergeQueue";
import { useMergeQueue, useMergeQueueChanges } from "@/newPullRequestPage/hooks/useMergeQueue";
import { currentWorkspaceAuthGeneration } from "@/lib/workspaceDraftOwner";
import { LifecycleNoticeView } from "@/pullRequestPage/LifecycleNoticeView";
import {
  MERGE_ON_GITHUB,
  mergeOnGitHub,
  mergeOnGitHubDescription,
  type NativeStackMergeHandoff,
} from "@/pullRequestPage/nativeStackMerge";
import { usePullRequestLifecycleActions } from "@/pullRequestPage/usePullRequestLifecycleActions";

/** Picker entries, as GitHub's merge menu names them. */
export const MERGE_METHOD_LABELS: Record<PullRequestMergeMethod, string> = {
  merge: "Create a merge commit",
  squash: "Squash and merge",
  rebase: "Rebase and merge",
};

/** Button labels for each method, as on GitHub's merge button. */
export const MERGE_BUTTON_LABELS: Record<PullRequestMergeMethod, string> = {
  merge: "Merge pull request",
  squash: "Squash and merge",
  rebase: "Rebase and merge",
};

/** GitHub's green merge button. */
const MERGE_BUTTON_CLASS = "bg-success text-success-foreground hover:bg-success/90";

/** Why GitHub won't merge the PR yet, by mergeStateStatus. */
const BLOCKED_HINTS: Record<string, string> = {
  BLOCKED: "Required checks or reviews haven’t passed.",
  BEHIND: "Update the branch first.",
  DIRTY: "Resolve conflicts first.",
  DRAFT: "Mark the pull request ready for review first.",
};

/** Caveats for states GitHub still merges, by mergeStateStatus. */
const READY_HINTS: Record<string, string> = {
  UNSTABLE: "Checks that aren’t required are failing or still running.",
  HAS_HOOKS: "The repository’s pre-receive hooks run on merge and can still reject it.",
};

// After this long, a requested merge or auto-merge change the refetch hasn't shown
// (say, a PR waiting in a merge queue) stops holding the button busy.
const REFRESH_TIMEOUT_MS = 30_000;

export type MergeButtonState =
  | { kind: "auto_merge" }
  | { kind: "checking"; hint: string }
  | { kind: "blocked"; hint: string }
  | { kind: "ready"; headSha: string; hint?: string };

/** A value that clears itself after {@link REFRESH_TIMEOUT_MS}. */
export function useExpiringValue<T>(): [T | null, (value: T) => void] {
  const [value, setValue] = useState<T | null>(null);
  useEffect(() => {
    if (value === null) return;
    const timer = setTimeout(() => setValue(null), REFRESH_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [value]);
  return [value, setValue];
}

/** What the Merge button shows for a PR, from its auto-merge and merge state. */
export function mergeButtonState(
  pr: Pick<PullRequest, "auto_merge" | "merge_state_status" | "head_sha"> &
    Partial<Pick<PullRequest, "is_draft">>,
): MergeButtonState {
  if (pr.auto_merge) return { kind: "auto_merge" };
  // GitHub no longer reports DRAFT as a merge state: a draft reads CLEAN, yet can't merge.
  if (pr.is_draft) return { kind: "blocked", hint: BLOCKED_HINTS.DRAFT };
  const status = pr.merge_state_status;
  const mergeable = status === "CLEAN" || (!!status && status in READY_HINTS);
  if (mergeable && pr.head_sha) {
    return { kind: "ready", headSha: pr.head_sha, hint: status ? READY_HINTS[status] : undefined };
  }
  // GitHub computes the state after a push; the next poll usually has it.
  if (!status || status === "UNKNOWN" || mergeable) {
    return { kind: "checking", hint: "Checking whether it can merge…" };
  }
  return {
    kind: "blocked",
    hint: BLOCKED_HINTS[status] ?? "GitHub reports that it can’t merge yet.",
  };
}

/** The dialog's method picker, or why there is nothing to pick. */
function MergeMethodPicker({
  options,
  method,
  disabled,
  onChange,
}: {
  options: ReturnType<typeof usePullRequestMergeOptions>;
  method: PullRequestMergeMethod | null;
  disabled: boolean;
  onChange: (method: PullRequestMergeMethod) => void;
}) {
  if (options.isPending) {
    return (
      <p role="status" className="text-ui text-muted-foreground">
        Loading merge methods…
      </p>
    );
  }
  if (options.isError) {
    return (
      <p role="alert" className="text-ui text-destructive">
        {options.error.message}
      </p>
    );
  }
  if (options.data.methods.length === 0) {
    return (
      <p role="alert" className="text-ui text-destructive">
        This repository allows no merge method.
      </p>
    );
  }
  return (
    <Select
      value={method ?? undefined}
      onValueChange={(value) => onChange(value as PullRequestMergeMethod)}
      disabled={disabled}
    >
      <SelectTrigger aria-label="Merge method" className="h-8 w-full text-ui">
        <SelectValue placeholder="Merge method" />
      </SelectTrigger>
      <SelectContent>
        {options.data.methods.map((value) => (
          <SelectItem key={value} value={value}>
            {MERGE_METHOD_LABELS[value]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** Confirms the merge and picks its method; the head is fixed when the dialog opens. */
export function PullRequestMergeDialog({
  conversationId,
  prUrl,
  pr,
  headSha,
  warning,
  notice,
  accounts,
  unavailable = false,
  onClose,
  onMerged,
}: {
  conversationId: string;
  prUrl: string;
  pr: PullRequest;
  headSha: string;
  /** A caveat GitHub still merges with, e.g. failing optional checks. */
  warning?: string;
  /** A page's own advice about merging, such as a stack's order; it never holds the merge. */
  notice?: ReactNode;
  /** The accounts the dialog offers to merge as; omit when the provider can't switch. */
  accounts?: MergeAccounts;
  unavailable?: boolean;
  onClose: () => void;
  onMerged: () => void;
}) {
  const options = usePullRequestMergeOptions(conversationId, prUrl, true);
  return (
    <DirectMergeDialog
      conversationId={conversationId}
      prUrl={prUrl}
      pr={pr}
      headSha={headSha}
      warning={warning}
      notice={notice}
      accounts={accounts}
      unavailable={unavailable}
      options={options}
      onClose={onClose}
      onMerged={onMerged}
    />
  );
}

/** The accounts a merge can run as, from the PR's info; omit when the provider can't switch. */
export type MergeAccounts = Pick<PullRequestAuth, "accounts" | "selected_account">;

/** Offers a merge-specific account choice, because read access doesn't imply merge permission. */
function MergeAccountPicker({
  options,
  selected,
  setPref,
  prUrl,
  disabled,
  onChange,
}: {
  options: PullRequestAccount[];
  selected: string | undefined;
  setPref: ReturnType<typeof useSetPullRequestPreference>;
  prUrl: string;
  disabled: boolean;
  onChange: () => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <span aria-hidden className="shrink-0 text-ui text-muted-foreground">
          Merge as
        </span>
        <Select
          // Stays controlled while no account is known, so the placeholder comes back
          // and choosing the same account again still saves it.
          value={selected ?? ""}
          onValueChange={(login) => {
            onChange();
            setPref.mutate({ account: login, pr_url: prUrl });
          }}
          disabled={disabled || setPref.isPending}
        >
          <SelectTrigger aria-label="Merge as" className="h-8 min-w-0 flex-1 text-ui">
            <SelectValue placeholder="Choose an account" />
          </SelectTrigger>
          <SelectContent>
            {options.map((account) => (
              <SelectItem key={account.login} value={account.login}>
                {account.login}
                {account.active ? " (active)" : ""}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {setPref.isError && (
        <p role="alert" className="text-ui text-destructive">
          Couldn’t switch accounts: {setPref.error.message}
        </p>
      )}
    </div>
  );
}

/** The merge confirmation: the method, the account, and the head the dialog opened with. */
function DirectMergeDialog({
  conversationId,
  prUrl,
  pr,
  headSha,
  warning,
  notice,
  accounts,
  unavailable,
  options,
  onClose,
  onMerged,
}: {
  conversationId: string;
  prUrl: string;
  pr: PullRequest;
  headSha: string;
  warning?: string;
  notice?: ReactNode;
  accounts?: MergeAccounts;
  unavailable: boolean;
  options: ReturnType<typeof usePullRequestMergeOptions>;
  onClose: () => void;
  onMerged: () => void;
}) {
  const merge = useMergePullRequest(conversationId);
  const setPref = useSetPullRequestPreference(conversationId);
  const accountOptions = accounts?.accounts ?? [];
  // Once the dialog has offered a choice, every merge from it is bound to an account,
  // even if a refetch later lists fewer accounts (an account lookup can fail alone).
  const [accountBound, setAccountBound] = useState(accountOptions.length > 1);
  if (!accountBound && accountOptions.length > 1) setAccountBound(true);
  // The host saved (or is saving) this account; the info refetch catches up after.
  const requestedAccount =
    setPref.isPending || setPref.isSuccess ? setPref.variables?.account : undefined;
  const shownAccount = requestedAccount ?? accounts?.selected_account ?? undefined;
  // What "Merge as" shows, which the merge runs as. An account the picker no longer
  // lists can't be shown, so it can't be merged as either.
  const mergeAccount =
    accountBound && accountOptions.some((account) => account.login === shownAccount)
      ? shownAccount
      : undefined;
  // E.g. after a failed switch, until the info refetch says which account is saved.
  const accountUnknown = accountBound && !mergeAccount;
  const busy = merge.isPending || setPref.isPending;
  const [chosen, setChosen] = useState<PullRequestMergeMethod | null>(null);
  const methods = options.data?.methods ?? [];
  const method = chosen && methods.includes(chosen) ? chosen : (options.data?.default ?? null);

  const methodLabel = method ? MERGE_BUTTON_LABELS[method] : "Merge pull request";
  // A saving account switch disables the button but doesn't merge, so it keeps the method's name.
  const merging = merge.isPending;
  const confirmLabel = merging ? "Merging…" : methodLabel;

  // `isPending` updates after a render, so a fast second click needs this guard.
  const submitting = useRef<number | null>(null);
  const confirm = () => {
    // Held only for the workspace user who clicked: a switch retires the request and this.
    if (!method || unavailable || busy || accountUnknown) return;
    if (submitting.current === currentWorkspaceAuthGeneration()) return;
    submitting.current = currentWorkspaceAuthGeneration();
    merge.mutate(
      { prUrl, mergeMethod: method, expectedHead: headSha, account: mergeAccount },
      {
        onSuccess: () => {
          submitting.current = null;
          onMerged();
          onClose();
        },
        onError: (error) => {
          submitting.current = null;
          // Cancel in main's sheet merged nothing; there's nothing to report.
          if (error instanceof PullRequestWriteError && error.code === "cancelled") merge.reset();
        },
      },
    );
  };

  return (
    <Dialog
      open
      // Keep the dialog up while a merge is in flight so its outcome stays visible,
      // and while an account switch saves, so a reopened dialog can't race it.
      onOpenChange={(next) => !next && !busy && onClose()}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Merge pull request #{pr.number}?</DialogTitle>
          <DialogDescription>
            {pr.title} will be merged into{" "}
            <span className="font-mono text-foreground">{pr.base_ref ?? "its base branch"}</span>.
            {" This can’t be undone."}
          </DialogDescription>
        </DialogHeader>
        {notice}
        {warning && (
          <p role="note" className="text-ui text-warning">
            {warning}
          </p>
        )}
        <MergeMethodPicker options={options} method={method} disabled={busy} onChange={setChosen} />
        {accountBound && (
          <MergeAccountPicker
            options={accountOptions}
            selected={mergeAccount}
            setPref={setPref}
            prUrl={prUrl}
            disabled={busy || unavailable}
            // The refusal was the previous account's.
            onChange={merge.reset}
          />
        )}
        {merge.isError && (
          <p role="alert" className="text-ui text-destructive">
            {merge.error?.message}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            className={MERGE_BUTTON_CLASS}
            disabled={!method || unavailable || busy || accountUnknown}
            onClick={confirm}
          >
            {merging && <Spinner aria-hidden />}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface AutoMergeProps {
  conversationId: string;
  prUrl: string;
  pr: PullRequest;
}

/**
 * What turning auto-merge off came to, outside a PR page, where nothing else says it. Mounted
 * whatever the bar shows: a refresh can read auto-merge off while its Check Again still holds.
 */
function AutoMergeDisableNotice({ conversationId, prUrl, pr }: AutoMergeProps) {
  const lifecycle = usePullRequestLifecycleActions({
    conversationId,
    prUrl,
    number: pr.number,
    state: pr.state,
    isDraft: pr.is_draft === true,
    headSha: pr.head_sha,
    autoMergeOn: !!pr.auto_merge,
    mergeable: false,
  });
  const { notice } = lifecycle.state;
  if (lifecycle.surface !== null || notice?.id !== "disable_auto_merge") return null;
  return (
    <LifecycleNoticeView
      notice={notice}
      prUrl={prUrl}
      checking={lifecycle.state.checking}
      checkFailed={lifecycle.state.checkFailed}
      onAction={lifecycle.runNoticeAction}
      className="mt-2"
    />
  );
}

/** Auto-merge is on: say so, and let the user turn it off on the session's host. */
function PullRequestAutoMerge({ conversationId, prUrl, pr }: AutoMergeProps) {
  const disable = useDisablePullRequestAutoMerge(conversationId);
  // The auto-merge GitHub turned off; it shows as on here until the refetch lands.
  const [disabledAt, setDisabledAt] = useExpiringValue<string>();
  const enabledAt = pr.auto_merge?.enabled_at ?? "";
  const busy = disable.isPending || disabledAt === enabledAt;
  // `isPending` updates after a render, so a fast second click needs this guard.
  const submitting = useRef<number | null>(null);
  const disableAutoMerge = () => {
    // Held only for the workspace user who clicked: a switch retires the request and this.
    if (busy || submitting.current === currentWorkspaceAuthGeneration()) return;
    submitting.current = currentWorkspaceAuthGeneration();
    disable.mutate(
      { prUrl },
      {
        onSuccess: () => {
          submitting.current = null;
          setDisabledAt(enabledAt);
        },
        onError: () => {
          submitting.current = null;
        },
      },
    );
  };
  return (
    <>
      <Button size="sm" variant="outline" disabled={busy} onClick={disableAutoMerge}>
        {busy && <Spinner aria-hidden />}
        Disable auto-merge
      </Button>
      <span className="text-ui text-muted-foreground">
        This pull request will <strong className="text-foreground">merge automatically</strong> when
        all requirements are met.
      </span>
      {disable.isError && (
        <p role="alert" className="w-full text-ui text-destructive">
          {disable.error?.message}
        </p>
      )}
    </>
  );
}

/** A queued PR's entry state, in words; GitHub's own name for any other. */
const QUEUE_ENTRY_LABELS: Record<string, string> = {
  QUEUED: "Queued",
  AWAITING_CHECKS: "Waiting for checks",
  MERGEABLE: "Ready to merge",
  UNMERGEABLE: "Can’t be merged",
  LOCKED: "Merging",
};

const QUEUE_OUTCOME_UNKNOWN = {
  add: "The app couldn’t tell whether GitHub added this pull request to the merge queue. Check the queue before trying again.",
  remove:
    "The app couldn’t tell whether GitHub removed this pull request from the merge queue. Check the queue before trying again.",
} as const;

/** What a queue change's failure says: why GitHub refused it, or that it may have been made. */
function queueChangeError(error: Error | null, change: "add" | "remove"): string | null {
  if (!error) return null;
  if (error instanceof PullRequestWriteError && error.outcomeUnknown)
    return QUEUE_OUTCOME_UNKNOWN[change];
  return error.message;
}

/** How a merge queue merges, as its confirmation names it. */
const QUEUE_METHOD_LABELS: Record<PullRequestMergeMethod, string> = {
  merge: "a merge commit",
  squash: "squash",
  rebase: "rebase",
};

/**
 * The confirmation before adding the PR to its base's merge queue, naming the PR, the branch
 * and the method. The host doesn't say the queue's method, so it's named only when the
 * repository allows just one, which the queue must then use.
 */
function MergeQueueAddDialog({
  pr,
  headSha,
  methods,
  add,
  unavailable,
  notice,
  onClose,
}: {
  pr: PullRequest;
  headSha: string;
  methods: readonly PullRequestMergeMethod[];
  add: NonNullable<ReturnType<typeof useMergeQueueChanges>["add"]>;
  unavailable: boolean;
  notice?: ReactNode;
  onClose: () => void;
}) {
  const busy = add.isPending;
  const only = methods.length === 1 ? methods[0]! : null;
  const failure = queueChangeError(add.error ?? null, "add");
  return (
    <Dialog open onOpenChange={(next) => !next && !busy && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add pull request #{pr.number} to the merge queue?</DialogTitle>
          <DialogDescription>
            The merge queue merges {pr.title} into{" "}
            <span className="font-mono text-foreground">{pr.base_ref ?? "its base branch"}</span>{" "}
            {only ? `with ${QUEUE_METHOD_LABELS[only]}` : "with the queue’s merge method"} once its
            checks pass.
          </DialogDescription>
        </DialogHeader>
        {notice}
        {failure && (
          <p role="alert" className="text-ui text-destructive">
            {failure}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={unavailable || busy}
            onClick={() => add.mutate(headSha, { onSuccess: onClose })}
          >
            {busy && <Spinner aria-hidden />}
            {busy ? "Adding…" : "Add to Merge Queue"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Where a queued PR stands, and Remove from Queue where the host serves it. */
function PullRequestMergeQueued({
  queue,
  remove,
}: {
  queue: MergeQueueState;
  remove: ReturnType<typeof useMergeQueueChanges>["remove"];
}) {
  const entry = queue.state ? (QUEUE_ENTRY_LABELS[queue.state] ?? queue.state) : null;
  const where = [queue.position ? `Position ${queue.position}` : null, entry]
    .filter(Boolean)
    .join(" · ");
  const failure = queueChangeError(remove?.error ?? null, "remove");
  return (
    <section className="flex flex-wrap items-center gap-2" aria-label="Merge">
      <p className="mr-auto min-w-0 text-ui text-muted-foreground">
        In the merge queue{where ? ` · ${where}` : ""}
      </p>
      {remove && (
        <Button
          data-merge-queue-remove
          size="sm"
          variant="outline"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          {remove.isPending && <Spinner aria-hidden />}
          {remove.isPending ? "Removing…" : "Remove from Queue"}
        </Button>
      )}
      {failure && (
        <p role="alert" className="basis-full text-ui text-destructive">
          {failure}
        </p>
      )}
    </section>
  );
}

/** What an enabled merge button does: merge the PR, or add it to its base's queue. */
export type MergeReadyAction = "merge" | "queue";

export function PullRequestMerge({
  conversationId,
  prUrl,
  pr,
  accounts,
  unavailable = false,
  readyCaption,
  beforeMerge,
  dialogNotice,
  handoff = null,
  stackPending = false,
}: {
  unavailable?: boolean;
  conversationId: string;
  prUrl: string;
  pr: PullRequest;
  /** A page's advice the merge dialogs show under their question, such as a stack's order. */
  dialogNotice?: ReactNode;
  /**
   * A page's caption, shown before the controls only while the button would act; the
   * controls then sit at the row's end.
   */
  readyCaption?: Record<MergeReadyAction, ReactNode>;
  /** A page's own control before Merge, where the base has no queue and nothing is armed. */
  beforeMerge?: ReactNode;
  /** A merge GitHub makes, for a PR in its own stack: the bar opens GitHub instead. */
  handoff?: NativeStackMergeHandoff | null;
  /** Whether GitHub may yet say it merges this PR with its stack: nothing merges until it does. */
  stackPending?: boolean;
  /** The accounts the dialog offers to merge as; omit when the provider can't switch. */
  accounts?: MergeAccounts;
}) {
  // The head the user confirms, kept while the dialog is open even if a poll moves it.
  const [confirmingHead, setConfirmingHead] = useState<string | null>(null);
  // GitHub accepted a merge of this head; the PR stays open here until the refetch lands.
  const [mergedHead, setMergedHead] = useExpiringValue<string>();
  const readiness = mergeButtonState(pr);
  // A merge GitHub makes, or may yet say it makes, can't be confirmed here: an unsent
  // confirmation is dropped, so it never comes back once the answer settles.
  const mergesHere = !handoff && !stackPending;
  if (confirmingHead && !mergesHere) setConfirmingHead(null);
  return (
    <>
      <PullRequestMergeSection
        conversationId={conversationId}
        prUrl={prUrl}
        pr={pr}
        unavailable={unavailable}
        mergedHead={mergedHead}
        onConfirm={setConfirmingHead}
        dialogNotice={dialogNotice}
        readyCaption={readyCaption}
        beforeMerge={beforeMerge}
        handoff={handoff}
        stackPending={stackPending}
      />
      <AutoMergeDisableNotice conversationId={conversationId} prUrl={prUrl} pr={pr} />
      {/* Kept open whatever the section shows meanwhile, until the user closes it. */}
      {confirmingHead && mergesHere && (
        <PullRequestMergeDialog
          conversationId={conversationId}
          prUrl={prUrl}
          pr={pr}
          headSha={confirmingHead}
          unavailable={unavailable}
          warning={readiness.kind === "ready" ? readiness.hint : undefined}
          notice={dialogNotice}
          accounts={accounts}
          onMerged={() => setMergedHead(confirmingHead)}
          onClose={() => setConfirmingHead(null)}
        />
      )}
    </>
  );
}

/** The Merge section itself, from the PR and the host's merge options. */
function PullRequestMergeSection({
  conversationId,
  prUrl,
  pr,
  unavailable,
  mergedHead,
  onConfirm,
  dialogNotice,
  readyCaption,
  beforeMerge,
  handoff,
  stackPending,
}: {
  conversationId: string;
  prUrl: string;
  pr: PullRequest;
  unavailable: boolean;
  mergedHead: string | null;
  onConfirm: (headSha: string) => void;
  dialogNotice?: ReactNode;
  readyCaption?: Record<MergeReadyAction, ReactNode>;
  beforeMerge?: ReactNode;
  handoff: NativeStackMergeHandoff | null;
  stackPending: boolean;
}) {
  // The button names the repository's default method; a merge GitHub makes needs no read.
  const options = usePullRequestMergeOptions(
    conversationId,
    prUrl,
    !pr.auto_merge && !handoff && !stackPending,
  );
  const queue = useMergeQueue(
    conversationId,
    prUrl,
    !pr.auto_merge && !handoff && !stackPending && pr.state?.toUpperCase() === "OPEN",
  );
  const queueChanges = useMergeQueueChanges(conversationId, prUrl);
  // The head the user is asked to queue, kept while the confirmation is open.
  const [queueHead, setQueueHead] = useState<string | null>(null);
  if (pr.auto_merge) {
    return (
      <section className="flex flex-wrap items-center gap-2" aria-label="Merge">
        <PullRequestAutoMerge conversationId={conversationId} prUrl={prUrl} pr={pr} />
      </section>
    );
  }
  if (handoff) {
    return (
      <section className="flex flex-wrap items-center justify-end gap-2" aria-label="Merge">
        <p className="mr-auto min-w-0 text-ui text-muted-foreground">
          {mergeOnGitHubDescription(handoff)}
        </p>
        <Button
          data-merge-on-github
          size="sm"
          variant="outline"
          onClick={() => mergeOnGitHub(handoff)}
        >
          <ExternalLinkIcon aria-hidden />
          {MERGE_ON_GITHUB.label}
        </Button>
      </section>
    );
  }
  if (queue?.queued) return <PullRequestMergeQueued queue={queue} remove={queueChanges.remove} />;
  // A base that merges through a queue takes the PR into it, where the host adds it.
  const add = queue?.enabled ? queueChanges.add : null;
  const state: MergeButtonState =
    mergedHead && mergedHead === pr.head_sha
      ? {
          kind: "checking",
          hint: "Merge requested. Refreshing…",
        }
      : stackPending
        ? { kind: "checking", hint: "Checking how it merges…" }
        : mergeButtonState(pr);
  const defaultMethod = options.data?.default;
  const label = add
    ? add.isPending
      ? "Adding…"
      : "Add to Merge Queue"
    : defaultMethod
      ? MERGE_BUTTON_LABELS[defaultMethod]
      : "Merge pull request";
  // The same decision that enables the button decides the caption, so they can't disagree.
  const caption =
    readyCaption && state.kind === "ready" && !unavailable
      ? readyCaption[add ? "queue" : "merge"]
      : null;
  const addFailure = queueChangeError(add?.error ?? null, "add");
  return (
    <section
      className={cn("flex flex-wrap items-center gap-2", readyCaption && "justify-end")}
      aria-label="Merge"
    >
      {caption && <div className="mr-auto min-w-0">{caption}</div>}
      {beforeMerge}
      <Button
        data-merge-button
        size="sm"
        variant={state.kind === "ready" ? "default" : "outline"}
        className={cn(state.kind === "ready" && MERGE_BUTTON_CLASS)}
        disabled={unavailable || state.kind !== "ready" || !!add?.isPending}
        onClick={() => {
          if (unavailable || state.kind !== "ready") return;
          if (add) {
            add.reset();
            setQueueHead(state.headSha);
          } else onConfirm(state.headSha);
        }}
      >
        {state.kind === "checking" || add?.isPending ? (
          <Spinner aria-hidden />
        ) : (
          <GitMergeIcon aria-hidden />
        )}
        {label}
      </Button>
      {"hint" in state && <span className="text-ui text-muted-foreground">{state.hint}</span>}
      {addFailure && !queueHead && (
        <p role="alert" className="basis-full text-ui text-destructive">
          {addFailure}
        </p>
      )}
      {add && queueHead && (
        <MergeQueueAddDialog
          pr={pr}
          headSha={queueHead}
          methods={options.data?.methods ?? []}
          add={add}
          unavailable={unavailable}
          notice={dialogNotice}
          onClose={() => setQueueHead(null)}
        />
      )}
    </section>
  );
}
