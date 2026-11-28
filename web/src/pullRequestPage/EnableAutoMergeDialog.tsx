// Turning auto-merge on is always confirmed here, since nothing asks again before it's sent. The
// question also asks how it should merge where the repository allows more than one way; a row
// reads this Mac's merge options first, and says why where it can't be turned on.

import { useRef, useState } from "react";

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
import {
  type PullRequestMergeMethod,
  type PullRequestMergeOptions,
  useMergeOptionsNow,
  usePullRequestMergeOptions,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { MERGE_METHOD_LABELS } from "@/newPullRequestPage/shell/PullRequestMerge";

import { lifecycleAvailability, MERGEABLE_NOW_MESSAGE } from "./pullRequestLifecycleActions";
import { focusOnOpen } from "./dialogFocus";

/**
 * Asks before turning auto-merge on, pinned to `headSha`, with the method chosen here where the
 * repository allows several and named where it allows one.
 */
export function EnableAutoMergeDialog({
  number,
  headSha,
  methods,
  initialMethod,
  onConfirm,
  onCancel,
}: {
  number: number;
  headSha: string;
  methods: readonly PullRequestMergeMethod[];
  initialMethod: PullRequestMergeMethod | null;
  onConfirm: (method: PullRequestMergeMethod) => void;
  onCancel: () => void;
}) {
  const [method, setMethod] = useState<PullRequestMergeMethod | null>(initialMethod);
  const cancelRef = useRef<HTMLButtonElement>(null);
  // One way to merge leaves nothing to choose, but turning it on is still asked.
  const only = methods.length === 1 ? methods[0]! : null;
  const chosen = only ?? method;
  return (
    <Dialog open onOpenChange={(next) => !next && onCancel()}>
      <DialogContent onOpenAutoFocus={focusOnOpen(cancelRef)}>
        <DialogHeader>
          <DialogTitle>Turn on auto-merge for #{number}?</DialogTitle>
          <DialogDescription>
            Auto-merge is turned on only if the branch is still at{" "}
            <code>{headSha.slice(0, 7)}</code>. GitHub then merges it once its required checks and
            reviews pass{only ? `, using “${MERGE_METHOD_LABELS[only]}”` : ", the way you choose"}.
          </DialogDescription>
        </DialogHeader>
        {!only && (
          <Select
            value={method ?? undefined}
            onValueChange={(value) => setMethod(value as PullRequestMergeMethod)}
          >
            <SelectTrigger aria-label="Merge method" className="h-8 w-full text-ui">
              <SelectValue placeholder="Merge method" />
            </SelectTrigger>
            <SelectContent>
              {methods.map((value) => (
                <SelectItem key={value} value={value}>
                  {MERGE_METHOD_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <DialogFooter>
          <Button ref={cancelRef} variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button disabled={!chosen || !headSha} onClick={() => chosen && onConfirm(chosen)}>
            Turn On Auto-merge
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Why this Mac's merge options say auto-merge can't be turned on, as main's codes name it. */
export type AutoMergeRefusal =
  | "already_on"
  | "merge_queue"
  | "merge_queue_unknown"
  | "no_method"
  | "mergeable_now"
  /** Any other state GitHub can't turn it on from now; main's own read names which. */
  | "not_waiting";

/** Why the merge options say auto-merge can't be turned on, or null where it can. */
export function autoMergeRefusalOf(
  options: PullRequestMergeOptions,
  pr: { url: string; headSha: string },
): AutoMergeRefusal | null {
  const availability = lifecycleAvailability("enable_auto_merge", {
    prUrl: pr.url,
    state: "OPEN",
    isDraft: false,
    mergeable: true,
    // Merge options don't say whether GitHub stacks the PR; the page decides that.
    mergesOnGitHub: false,
    headSha: pr.headSha,
    autoMergeOn: !!options.auto_merge,
    macMerge: {
      queue: options.merge_queue,
      mergeState: options.merge_state_status,
      defaultMethod: options.default ?? null,
    },
    offered: () => true,
    held: false,
  });
  if (availability.kind !== "hidden") return null;
  if (options.auto_merge) return "already_on";
  if (options.merge_queue === true) return "merge_queue";
  if (options.merge_queue !== false) return "merge_queue_unknown";
  if (!options.default || options.methods.length === 0) return "no_method";
  const state = options.merge_state_status?.toUpperCase();
  if (state === "CLEAN" || state === "UNSTABLE" || state === "HAS_HOOKS") return "mergeable_now";
  return "not_waiting";
}

const REFUSAL_MESSAGES: Record<AutoMergeRefusal, string> = {
  already_on: "Auto-merge is already on.",
  merge_queue: "Its base branch merges through a merge queue. Use Merge… to add it to the queue.",
  merge_queue_unknown:
    "GitHub couldn’t say whether its base branch uses a merge queue. Turn auto-merge on from GitHub.",
  no_method: "The repository allows no merge method to turn auto-merge on with.",
  mergeable_now: MERGEABLE_NOW_MESSAGE,
  not_waiting: "GitHub can’t turn on auto-merge for it now. Check it on GitHub.",
};

/** Why this Mac's merge options say auto-merge can't be turned on, in words, or null. */
export function autoMergeRefusedBy(
  options: PullRequestMergeOptions,
  pr: { url: string; headSha: string },
): string | null {
  const refusal = autoMergeRefusalOf(options, pr);
  return refusal ? REFUSAL_MESSAGES[refusal] : null;
}

/**
 * A list row's Enable Auto-merge…: reads this Mac's merge options for the PR first, then asks
 * with them, or says why it can't be turned on, sending nothing.
 */
export function RowEnableAutoMergeDialog({
  conversationId,
  prUrl,
  number,
  headSha,
  onConfirm,
  onCancel,
}: {
  conversationId: string;
  prUrl: string;
  number: number;
  headSha: string;
  /** The method chosen, with the options the dialog asked with, to check again as it's sent. */
  onConfirm: (method: PullRequestMergeMethod, options: PullRequestMergeOptions) => void;
  onCancel: () => void;
}) {
  const options = usePullRequestMergeOptions(conversationId, prUrl, true);
  const closeRef = useRef<HTMLButtonElement>(null);
  // A read that failed since outranks the answer before it, which may no longer hold.
  const data = options.isError ? undefined : options.data;
  // Turn On sends only from the read as it stands when clicked, not as it was rendered.
  const optionsNow = useMergeOptionsNow(conversationId, prUrl);
  if (data && !autoMergeRefusedBy(data, { url: prUrl, headSha })) {
    return (
      <EnableAutoMergeDialog
        number={number}
        headSha={headSha}
        methods={data.methods}
        initialMethod={data.default ?? null}
        onConfirm={(method) => {
          const now = optionsNow();
          if (!now || autoMergeRefusedBy(now, { url: prUrl, headSha })) return;
          onConfirm(method, now);
        }}
        onCancel={onCancel}
      />
    );
  }
  const refused = data ? autoMergeRefusedBy(data, { url: prUrl, headSha }) : null;
  return (
    <Dialog open onOpenChange={(next) => !next && onCancel()}>
      <DialogContent onOpenAutoFocus={focusOnOpen(closeRef)}>
        <DialogHeader>
          <DialogTitle>Turn on auto-merge for #{number}?</DialogTitle>
          <DialogDescription>
            {options.isError
              ? `Couldn’t read how #${number} merges: ${options.error.message}`
              : (refused ?? `Reading how #${number} merges…`)}
          </DialogDescription>
        </DialogHeader>
        {!data && !options.isError && <Spinner aria-label="Reading" />}
        <DialogFooter>
          {options.isError && (
            <Button variant="outline" onClick={() => void options.refetch()}>
              Try Again
            </Button>
          )}
          <Button ref={closeRef} variant={refused ? "default" : "outline"} onClick={onCancel}>
            {refused ? "OK" : "Cancel"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
