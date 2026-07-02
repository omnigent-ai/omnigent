/**
 * Commands wiring the Sessions tree to the editor panel.
 *
 * Thin VS Code adapter — the provider and the pure modules hold the logic.
 * Local-only slice: clicking a session deep-links the editor-beside iframe to
 * `/c/<id>`; the title-bar Refresh re-fetches the list.
 */
import * as vscode from "vscode";
import type { EditorPanelController } from "../panel/EditorPanelController";
import type { SessionsTreeProvider } from "../sessions/SessionsTreeProvider";

export const OPEN_SESSION_FROM_TREE_COMMAND = "omnigent.openSessionFromTree";
export const REFRESH_SESSIONS_COMMAND = "omnigent.sessions.refresh";

/**
 * Register the command fired when a tree item is clicked: navigate the editor
 * panel to the session route.
 */
export function registerOpenSessionFromTree(
  context: vscode.ExtensionContext,
  controller: EditorPanelController,
  output: vscode.OutputChannel,
): void {
  const cmd = vscode.commands.registerCommand(
    OPEN_SESSION_FROM_TREE_COMMAND,
    (id: string) => {
      if (!id) return;
      output.appendLine(`[omnigent] sessions: open ${id} from tree`);
      controller.navigate(`/c/${id}`);
    },
  );
  context.subscriptions.push(cmd);
}

/** Register the tree title-bar Refresh action. */
export function registerRefreshSessions(
  context: vscode.ExtensionContext,
  provider: SessionsTreeProvider,
): void {
  const cmd = vscode.commands.registerCommand(REFRESH_SESSIONS_COMMAND, () => {
    void provider.refresh();
  });
  context.subscriptions.push(cmd);
}
