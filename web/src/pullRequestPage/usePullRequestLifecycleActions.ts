// Runs the new page's draft and auto-merge actions for one PR: where each stands, opening and
// confirming their dialogs, sending each change bound to what the click captured, and what it
// came to. Every entry point (state pill, Overview link, merge bar, ⌘K) goes through this.

import { usePullRequestChangesHere } from "@/pullRequestPage/pullRequestChangesHere";
import { usePullRequestActionOrigin } from "@/lib/pullRequestActionOrigin";
import { useQueryClient } from "@tanstack/react-query";
import { useContext } from "react";

import {
  type PullRequestMergeMethod,
  useEnablePullRequestAutoMerge,
  usePullRequestAutoMergeDisableChange,
  usePullRequestDraftChange,
  usePullRequestMergeOptions,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";

import {
  LIFECYCLE_ACTION_IDS,
  LifecycleSurfaceContext,
  type LifecycleActionDefinition,
  type LifecycleActionId,
  type LifecycleActionState,
  type LifecycleAvailability,
  type LifecycleNoticeAction,
  lifecycleActionState,
  lifecycleAvailability,
  updateLifecycleActionState,
  useLifecycleActionState,
} from "./pullRequestLifecycleActions";
import {
  checkLifecycleChangeAgain,
  type LifecycleChanges,
  lifecycleHeld,
  requestLifecycleChange,
  sendLifecycleChange,
} from "./lifecycleRequests";
import { hostLifecycleRoute } from "./hostLifecycleRoute";
import type { NativeStackMergeHandoff } from "./nativeStackMerge";
import { stateChainKey, useStateChainState } from "./pullRequestStateChain";

/** The PR as the page shows it, for deciding which actions apply. */
export interface LifecyclePullRequest {
  conversationId: string;
  prUrl: string;
  number: number;
  state: string | null | undefined;
  isDraft: boolean;
  headSha: string | null | undefined;
  autoMergeOn: boolean;
  /** Whether the merge bar shows for this PR, so merging is offered from here at all. */
  mergeable: boolean;
  /** Where GitHub merges this PR with its stack: Merge hands off there, and nothing arms here. */
  mergeOnGitHub?: NativeStackMergeHandoff | null;
  /** False for another provider's PR, such as Azure DevOps': there's no GitHub to hand off to. */
  onGitHub?: boolean;
}

export interface PullRequestLifecycleActions {
  /** The workspace user and PR these act for. */
  key: string;
  /** The page surface these render on, or null outside a page. */
  surface: string | null;
  availability: Record<LifecycleActionId, LifecycleAvailability>;
  state: LifecycleActionState;
  /** Start `id`: open its confirmation, or for Ready for review, make it now. */
  request: (id: LifecycleActionId) => void;
  /** Confirm the open dialog; auto-merge with the method chosen there. */
  confirm: (method?: PullRequestMergeMethod) => void;
  cancelConfirm: () => void;
  methods: readonly PullRequestMergeMethod[];
  defaultMethod: PullRequestMergeMethod | null;
  checkAgain: () => Promise<void>;
  dismiss: () => void;
  /** Run one of the notice's actions, from its button or its ⌘K row. */
  runNoticeAction: (action: LifecycleNoticeAction) => void;
}

/** Whether a confirmation may still be shown and sent: the page still makes the change. */
export function confirmationApplies(availability: LifecycleAvailability): boolean {
  return availability.kind === "mac" || availability.kind === "held";
}

export function usePullRequestLifecycleActions(
  pr: LifecyclePullRequest,
): PullRequestLifecycleActions {
  const owner = useWorkspaceDraftOwner();
  const surface = useContext(LifecycleSurfaceContext);
  const key = stateChainKey(owner, pr.prUrl);
  const state = useLifecycleActionState(key);
  // Re-rendered as close or reopen runs, since it holds these too.
  useStateChainState(key);
  const queryClient = useQueryClient();
  const convert = usePullRequestDraftChange(pr.conversationId, "pull_request_convert_to_draft");
  const ready = usePullRequestDraftChange(pr.conversationId, "pull_request_ready_for_review");
  const enable = useEnablePullRequestAutoMerge(pr.conversationId);
  const disable = usePullRequestAutoMergeDisableChange(pr.conversationId);
  const origin = usePullRequestActionOrigin();
  // A view that may not change its PR, as for anyone but the session's owner, hands off to GitHub.
  const canChange = usePullRequestChangesHere();
  const hostMakes = (operation: LifecycleActionDefinition["operation"]) =>
    canChange && !!hostLifecycleRoute(pr.conversationId, operation);
  const viaHost = hostMakes("pull_request_auto_merge_enable");
  const open = pr.state?.toUpperCase() === "OPEN";
  // The merge read the merge bar shares says whether the base has a queue.
  const options = usePullRequestMergeOptions(
    pr.conversationId,
    pr.prUrl,
    viaHost && open && !pr.isDraft && !pr.autoMergeOn && pr.mergeable && !pr.mergeOnGitHub,
  );
  const methods = options.data?.methods ?? [];
  const defaultMethod = options.data?.default ?? null;
  const facts = {
    prUrl: pr.prUrl,
    state: pr.state,
    isDraft: pr.isDraft,
    mergeable: pr.mergeable,
    mergesOnGitHub: !!pr.mergeOnGitHub,
    headSha: pr.headSha,
    autoMergeOn: pr.autoMergeOn || !!options.data?.auto_merge,
    macMerge:
      viaHost && options.data
        ? {
            queue: options.data.merge_queue,
            mergeState: options.data.merge_state_status,
            defaultMethod,
          }
        : null,
    onGitHub: pr.onGitHub,
    offered: hostMakes,
    held: lifecycleHeld(key),
  };
  const availability = Object.fromEntries(
    LIFECYCLE_ACTION_IDS.map((id) => [id, lifecycleAvailability(id, facts)]),
  ) as Record<LifecycleActionId, LifecycleAvailability>;

  const target = {
    key,
    owner,
    prUrl: pr.prUrl,
    number: pr.number,
    conversationId: pr.conversationId,
  };
  const changes: LifecycleChanges = {
    convert,
    ready,
    enable,
    disable,
    toasts: true,
    origin,
  };

  const request = (id: LifecycleActionId) => {
    // The page says what it came to in its notice and toasts; nothing waits on it here.
    void requestLifecycleChange(
      target,
      id,
      { availability: availability[id], surface, headSha: pr.headSha, defaultMethod },
      changes,
    );
  };

  const confirm = (method?: PullRequestMergeMethod) => {
    const confirming = lifecycleActionState(key).confirming;
    if (!confirming || confirming.surface !== surface) return;
    // What the dialog opened for may no longer apply, e.g. once GitHub says it merges a stack.
    if (!confirmationApplies(availability[confirming.id])) {
      cancelConfirm();
      return;
    }
    if (confirming.id === "enable_auto_merge" && method && !methods.includes(method)) return;
    void sendLifecycleChange(
      target,
      confirming.id,
      {
        capture: confirming.capture,
        headSha: confirming.headSha,
        method: method ?? confirming.method,
      },
      changes,
    );
  };

  const cancelConfirm = () =>
    updateLifecycleActionState(key, (current) =>
      current.confirming?.surface === surface ? { ...current, confirming: null } : current,
    );

  const checkAgain = async () => {
    await checkLifecycleChangeAgain(queryClient, pr.conversationId, key, pr.prUrl);
  };

  const dismiss = () =>
    updateLifecycleActionState(key, (current) =>
      // An unknown outcome stays until a fresh read; only the others can be dismissed.
      current.notice?.outcome.kind === "unknown" ? current : { ...current, notice: null },
    );

  const runNoticeAction = (action: LifecycleNoticeAction) => {
    if (action === "check") void checkAgain();
    else if (action === "dismiss") dismiss();
    else window.open(pr.prUrl, "_blank", "noreferrer");
  };

  return {
    runNoticeAction,
    key,
    surface,
    availability,
    state,
    request,
    confirm,
    cancelConfirm,
    methods,
    defaultMethod,
    checkAgain,
    dismiss,
  };
}
