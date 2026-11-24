// The tabs sidebar as a session view or grid cell hosts it: the panel plus what the classic
// rail's host doesn't provide, which are the shell close question, ending a shell, and Back
// and Forward. Loaded on demand, so the classic sidebar never downloads it.

import { useCallback, useMemo } from "react";

import { useDeleteTerminal } from "@/hooks/useTerminals";
import { useValueAfterDeferredRender } from "@/hooks/useValueAfterDeferredRender";
import type { TerminalInfo } from "@/lib/terminals";

import { CloseShellDialog } from "../CloseShellDialog";
import type { WorkspaceHistoryDirection } from "../workspaceCommands/model";
import { useWorkspaceHistoryCommands } from "../workspaceCommands/useWorkspaceHistoryCommands";
import { CloseShellsDialog } from "./CloseShellsDialog";
import { useTabsShellClose } from "./useTabsShellClose";
import { useWorkspaceHistory } from "./useWorkspaceHistory";
import { WorkspaceTabsPanel, type WorkspaceTabsPanelProps } from "./WorkspaceTabsPanel";

export type WorkspaceTabsHostProps = Omit<
  WorkspaceTabsPanelProps,
  "confirmCloseShell" | "confirmCloseShells" | "endShell" | "historyCommands" | "onHistoryGo"
> & {
  /** The session's terminals, which a shell tab's close ends. */
  terminals: readonly TerminalInfo[];
  /**
   * Whether this sidebar binds Back and Forward; one owner at a time, so a grid passes it
   * for its selected cell only. Defaults to true.
   */
  bindsHistory?: boolean;
};

export function WorkspaceTabsHost({
  terminals,
  bindsHistory = true,
  ...panelProps
}: WorkspaceTabsHostProps) {
  const { conversationId, onReveal } = panelProps;
  // Going inert restyles everything in the rail, so a collapse takes it in a frame of its own,
  // after the toggle's. Leaving inert stays immediate, so what an expand focuses can take focus.
  const inertNow = panelProps.inert ?? false;
  const inertLater = useValueAfterDeferredRender(inertNow);
  // The sidebar names the shells a close couldn't end in one message of its own.
  const deleteTerminal = useDeleteTerminal(conversationId, { reportsFailure: false });
  const { mutateAsync: deleteTerminalAsync } = deleteTerminal;
  const shellClose = useTabsShellClose({ terminals, deleteTerminal: deleteTerminalAsync });

  const historySessionId = bindsHistory ? conversationId : null;
  const history = useWorkspaceHistory(historySessionId);
  const { go: goInHistory } = history;
  // Back and Forward work while the sidebar is closed, opening it first.
  const go = useCallback(
    (direction: WorkspaceHistoryDirection) => {
      onReveal?.();
      goInHistory(direction);
    },
    [onReveal, goInHistory],
  );
  const historyCommands = useWorkspaceHistoryCommands({
    conversationId: historySessionId,
    facts: history.facts,
    go,
  });
  const historyProps = useMemo(
    () => (bindsHistory ? { historyCommands, onHistoryGo: go } : {}),
    [bindsHistory, historyCommands, go],
  );

  return (
    <>
      <WorkspaceTabsPanel
        {...panelProps}
        {...historyProps}
        inert={inertNow && inertLater}
        inertPending={inertNow && !inertLater}
        confirmCloseShell={shellClose.confirmCloseShell}
        confirmCloseShells={shellClose.confirmCloseShells}
        endShell={shellClose.endShell}
      />
      <CloseShellsDialog
        open={(shellClose.question?.count ?? 0) > 1}
        shellLabels={shellClose.question?.label ?? null}
        shellCount={shellClose.question?.count ?? 0}
        onCancel={() => shellClose.answerQuestion(false)}
        onConfirm={() => shellClose.answerQuestion(true)}
      />
      <CloseShellDialog
        open={shellClose.question !== null && shellClose.question.count === 1}
        shellLabel={shellClose.question?.label ?? null}
        onCancel={() => shellClose.answerQuestion(false)}
        onConfirm={() => shellClose.answerQuestion(true)}
      />
    </>
  );
}

export default WorkspaceTabsHost;
