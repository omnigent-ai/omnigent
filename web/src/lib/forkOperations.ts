// Cross-module coordination for async fork operations.
//
// Fork materialization is a background server operation: the POST returns an
// `operation_id`, and a `fork_status` event on the session-updates stream later
// reports `ready` (with the destination `fork_id`) or `failed`. Two pieces of
// state need to bridge the fork dialog (which unmounts on close) and the
// always-mounted SessionUpdatesProvider that receives those events:
//
//   1. A pending coding-fork RUNNER BIND. A non-sandbox coding fork must bind a
//      runner once its session exists, but the id only arrives with `ready`, so
//      the dialog stashes the bind params here and the provider fires them then.
//   2. A REOPEN opener so the failure toast's "Try again" can bring the fork
//      dialog back for the source session (AppShell owns the dialog; it
//      registers the opener here at mount).

import type { LaunchRunnerGitOptions } from "@/lib/sessionsApi";

/** Runner-bind params captured at fork time, minus the not-yet-known fork id. */
export interface PendingForkBind {
  hostId: string;
  workspace: string;
  git?: LaunchRunnerGitOptions;
}

const pendingBinds = new Map<string, PendingForkBind>();

/** Stash a coding fork's runner bind to fire when its `ready` event arrives. */
export function registerPendingForkBind(operationId: string, bind: PendingForkBind): void {
  pendingBinds.set(operationId, bind);
}

/** Pop the pending bind for an operation (returns undefined for sandbox/chat forks). */
export function takePendingForkBind(operationId: string): PendingForkBind | undefined {
  const bind = pendingBinds.get(operationId);
  pendingBinds.delete(operationId);
  return bind;
}

type ReopenForkDialog = (sourceId: string) => void;

let reopenForkDialog: ReopenForkDialog | null = null;

/** AppShell registers its dialog opener so a failure toast can reopen the fork. */
export function setForkDialogReopener(reopen: ReopenForkDialog | null): void {
  reopenForkDialog = reopen;
}

/** Reopen the fork dialog for a source session; no-op if none is registered. */
export function reopenForkDialogForSource(sourceId: string): void {
  reopenForkDialog?.(sourceId);
}
