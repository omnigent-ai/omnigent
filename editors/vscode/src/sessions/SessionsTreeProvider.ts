/**
 * Thin VS Code adapter exposing the local Omnigent session list as a native
 * TreeView. All sorting/view-model logic lives in the pure `treeItem.ts` module;
 * this class owns only the IDE wiring and the load lifecycle.
 *
 * Local-only slice: no auth, so there is no `unauthorized` state; no filtering
 * and no background polling (manual Refresh + refresh-on-visible only).
 */
import * as vscode from "vscode";
import { listSessions, type ClientOptions, type Session } from "../api/client";
import { sortSessions, toItemView } from "./treeItem";

export const SESSIONS_VIEW_ID = "omnigent.sessions";

/** Default ceiling for accumulated sessions (mirrors `listSessions` default). */
const SESSIONS_CAP = 200;

export type SessionsState = "loading" | "ready" | "error" | "no-server";

/** A tree node: either a real session or a single non-selectable message line. */
export type SessionsNode =
  | { kind: "session"; session: Session }
  | { kind: "message"; label: string };

export class SessionsTreeProvider implements vscode.TreeDataProvider<SessionsNode> {
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<SessionsNode | undefined | void>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private state: SessionsState = "loading";
  private sessions: Session[] = [];

  constructor(
    private readonly getClientOpts: () => ClientOptions | undefined,
    private readonly output: vscode.OutputChannel,
  ) {}

  /** Re-fetch sessions, update state, and fire a tree change. */
  async refresh(): Promise<void> {
    const opts = this.getClientOpts();
    if (!opts) {
      this.apply("no-server", []);
      return;
    }

    this.state = "loading";
    this._onDidChangeTreeData.fire();

    const res = await listSessions(opts, SESSIONS_CAP);
    if (!res.ok || !res.data) {
      this.output.appendLine(
        `[omnigent] sessions: list failed (${res.status}: ${res.error ?? "unknown"})`,
      );
      this.apply("error", []);
    } else {
      this.apply("ready", res.data.sessions);
    }
  }

  /** Commit a fetch result and fire a tree change. */
  private apply(state: SessionsState, sessions: Session[]): void {
    this.state = state;
    this.sessions = sessions;
    this._onDidChangeTreeData.fire();
  }

  getTreeItem(node: SessionsNode): vscode.TreeItem {
    if (node.kind === "message") {
      const item = new vscode.TreeItem(node.label, vscode.TreeItemCollapsibleState.None);
      item.contextValue = "omnigentMessage";
      return item;
    }
    const view = toItemView(node.session, Date.now());
    const item = new vscode.TreeItem(view.label, vscode.TreeItemCollapsibleState.None);
    item.id = view.id;
    item.description = view.description;
    item.tooltip = new vscode.MarkdownString(view.tooltip);
    item.iconPath = new vscode.ThemeIcon(view.themeIconId);
    item.contextValue = view.contextValue;
    item.command = {
      command: "omnigent.openSessionFromTree",
      title: "Open Session",
      arguments: [view.id],
    };
    return item;
  }

  getChildren(element?: SessionsNode): SessionsNode[] {
    // Flat list — sessions have no children.
    if (element) return [];

    if (this.state === "loading") return [message("Loading…")];
    if (this.state === "no-server" || this.state === "error") {
      return [message("Omnigent server unreachable")];
    }
    if (this.sessions.length === 0) return [message("No sessions")];

    return sortSessions(this.sessions).map((session) => ({ kind: "session", session }));
  }
}

function message(label: string): SessionsNode {
  return { kind: "message", label };
}
