// The Pull Requests panel's own dialogs, as the desktop diagnostics log names them: which one
// showed and how it ended, so a report can be traced from the log. Never a repository, pull
// request, account, head or the dialog's words.

import { useEffect, useRef } from "react";

import { reportPullRequestDialog } from "@/lib/nativeBridge";

/** The dialogs main accepts records for; any other name is dropped there. */
export const PULL_REQUEST_DIALOGS = [
  "merge_queue_add",
  "merge_queue_queued",
  "merge_queue_elsewhere",
  "merge_queue_unconfirmed",
  "merge_not_ready",
] as const;

export type PullRequestDialogName = (typeof PULL_REQUEST_DIALOGS)[number];

/**
 * Report `dialog` shown while mounted, and dismissed when it goes away, unless `continued()`
 * says the user went on through it first.
 */
export function usePullRequestDialogReport(dialog: PullRequestDialogName): {
  continued: () => void;
} {
  const went = useRef(false);
  useEffect(() => {
    went.current = false;
    reportPullRequestDialog({ dialog, outcome: "shown" });
    return () => {
      if (!went.current) reportPullRequestDialog({ dialog, outcome: "dismissed" });
    };
  }, [dialog]);
  return {
    continued: () => {
      if (went.current) return;
      went.current = true;
      reportPullRequestDialog({ dialog, outcome: "continued" });
    },
  };
}
