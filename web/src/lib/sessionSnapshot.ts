import type { SessionStatusEvent } from "@/lib/events";
import { REQUESTED_HOST_ID_LABEL } from "@/lib/sessionHost";
import {
  acceptSessionProjection,
  isVersionedSessionObservation,
  preserveSessionErrorLabels,
} from "@/lib/sessionObservation";
import type { Session } from "@/lib/types";

const latestSessionStatuses = new Map<string, SessionStatusEvent>();

export function clearSessionSnapshotStatuses(): void {
  latestSessionStatuses.clear();
}

/** Reconcile lifecycle ownership while allowing independent snapshot metadata to refresh. */
export function mergeSessionSnapshot(previous: Session | undefined, incoming: Session): Session {
  const merged = mergeSnapshotData(previous, incoming);
  const live = latestSessionStatuses.get(incoming.id);
  return live ? applySessionStatus(merged, live) : merged;
}

function mergeSnapshotData(previous: Session | undefined, incoming: Session): Session {
  if (
    !previous ||
    acceptSessionProjection(
      incoming.sessionObservation,
      incoming.runnerOfflineEpisodeId,
      previous.sessionObservation,
      previous.runnerOfflineEpisodeId,
    )
  ) {
    return incoming;
  }
  const sameBinding =
    isVersionedSessionObservation(incoming.sessionObservation) &&
    incoming.sessionObservation.binding_epoch === previous.sessionObservation?.binding_epoch &&
    incoming.sessionObservation.runner_id === previous.sessionObservation?.runner_id;
  const labels = preserveSessionErrorLabels(
    sameBinding
      ? (incoming.labels ?? previous.labels ?? {})
      : withoutRequestedHost(incoming.labels ?? previous.labels ?? {}),
    previous.labels,
  );
  if (!sameBinding) {
    if (previous.labels?.[REQUESTED_HOST_ID_LABEL]) {
      labels[REQUESTED_HOST_ID_LABEL] = previous.labels[REQUESTED_HOST_ID_LABEL];
    }
  }
  return {
    ...incoming,
    status: previous.status,
    sessionObservation: previous.sessionObservation,
    runnerOfflineEpisodeId: previous.runnerOfflineEpisodeId,
    lastTaskError: previous.lastTaskError,
    activeResponseId: previous.activeResponseId,
    backgroundTaskCount: previous.backgroundTaskCount,
    backgroundTasks: previous.backgroundTasks,
    pendingElicitations: previous.pendingElicitations,
    pendingInputs: previous.pendingInputs,
    terminalPending: previous.terminalPending,
    sandboxStatus: previous.sandboxStatus,
    mcpStartup: previous.mcpStartup,
    runnerId: previous.runnerId,
    runnerOnline: sameBinding
      ? (incoming.runnerOnline ?? previous.runnerOnline)
      : previous.runnerOnline,
    hostId: sameBinding ? (previous.hostId ?? incoming.hostId) : previous.hostId,
    hostOnline: sameBinding ? (incoming.hostOnline ?? previous.hostOnline) : previous.hostOnline,
    hostResumable: sameBinding
      ? (incoming.hostResumable ?? previous.hostResumable)
      : previous.hostResumable,
    labels,
  };
}

/** TanStack applies this against the cache that exists when a fetch settles. */
export function shareSessionSnapshot(previous: unknown, incoming: unknown): Session {
  return mergeSessionSnapshot(previous as Session | undefined, incoming as Session);
}

export function sessionLabelsFromStatus(
  previous: Record<string, string> | undefined,
  event: SessionStatusEvent,
  bindingChanged: boolean,
): Record<string, string> {
  const labels = preserveSessionErrorLabels(
    bindingChanged ? withoutRequestedHost(previous ?? {}) : (previous ?? {}),
    undefined,
  );
  if (event.status === "failed" && event.error) {
    for (const key of ["code", "message", "title", "cause", "remediation"] as const) {
      const value = event.error[key];
      if (value !== undefined) labels[`omnigent.last_task_error_${key}`] = value;
    }
  }
  return labels;
}

/** Learn live ownership in the same snapshot cache read by hooks and graph roots. */
export function sessionSnapshotFromStatus(
  previous: Session | undefined,
  event: SessionStatusEvent,
): Session | undefined {
  const known = latestSessionStatuses.get(event.conversationId);
  if (
    known &&
    !acceptSessionProjection(
      event.sessionObservation,
      event.runnerOfflineEpisodeId,
      known.sessionObservation,
      known.runnerOfflineEpisodeId,
    )
  )
    return previous;
  latestSessionStatuses.set(event.conversationId, event);
  return previous ? applySessionStatus(previous, event) : previous;
}

function applySessionStatus(previous: Session, event: SessionStatusEvent): Session {
  if (
    !acceptSessionProjection(
      event.sessionObservation,
      event.runnerOfflineEpisodeId,
      previous.sessionObservation,
      previous.runnerOfflineEpisodeId,
    )
  ) {
    return previous;
  }
  const bindingChanged =
    isVersionedSessionObservation(event.sessionObservation) &&
    (event.sessionObservation.binding_epoch !== previous.sessionObservation?.binding_epoch ||
      event.sessionObservation.runner_id !== previous.sessionObservation?.runner_id);
  const labels = sessionLabelsFromStatus(previous.labels, event, bindingChanged);
  const ownershipChanged =
    isVersionedSessionObservation(event.sessionObservation) &&
    !acceptSessionProjection(
      previous.sessionObservation,
      previous.runnerOfflineEpisodeId,
      event.sessionObservation,
      event.runnerOfflineEpisodeId,
    );
  return {
    ...previous,
    status: event.status,
    sessionObservation: event.sessionObservation ?? previous.sessionObservation,
    runnerOfflineEpisodeId: event.runnerOfflineEpisodeId ?? null,
    lastTaskError: event.status === "failed" ? (event.error ?? null) : null,
    runnerId: event.sessionObservation ? event.sessionObservation.runner_id : previous.runnerId,
    ...(bindingChanged
      ? { hostId: null, runnerOnline: undefined, hostOnline: undefined, hostResumable: false }
      : {}),
    ...(ownershipChanged
      ? {
          pendingElicitations: [],
          pendingInputs: [],
          terminalPending: false,
          sandboxStatus: null,
          mcpStartup: null,
        }
      : {}),
    activeResponseId:
      event.status === "running" || event.status === "waiting"
        ? (event.responseId ?? event.sessionObservation?.response_id ?? previous.activeResponseId)
        : null,
    backgroundTaskCount:
      event.backgroundTaskCount ??
      (bindingChanged || event.status === "failed" ? 0 : previous.backgroundTaskCount),
    backgroundTasks:
      event.backgroundTaskCount !== undefined
        ? event.backgroundTaskCount > 0
          ? (event.backgroundTasks ?? [])
          : []
        : bindingChanged || event.status === "failed"
          ? []
          : previous.backgroundTasks,
    labels,
  };
}

function withoutRequestedHost(labels: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(labels).filter(([key]) => key !== REQUESTED_HOST_ID_LABEL),
  );
}
