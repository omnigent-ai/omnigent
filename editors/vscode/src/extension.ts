/**
 * Omnigent VS Code extension entry point (iframe + sessions sidebar).
 *
 * activate() wires:
 *  - Config / local-server discovery
 *  - A native Sessions tree view (omnigent.sessions) listing the local server's
 *    sessions; clicking one deep-links the editor panel to /c/<id>
 *  - EditorPanelController: the single editor-beside iframe surface
 *  - The omnigent.open command + the sessions refresh / open-from-tree commands
 */
import * as vscode from "vscode";
import { discoverLocalServer, DEFAULT_HEALTH_TIMEOUT_MS } from "./discovery";
import { resolveServerTarget } from "./config";
import { readSettings } from "./config/vscodeSettings";
import { EditorPanelController } from "./panel/EditorPanelController";
import { registerOpenPanel } from "./commands/openPanel";
import { SessionsTreeProvider, SESSIONS_VIEW_ID } from "./sessions/SessionsTreeProvider";
import {
  registerOpenSessionFromTree,
  registerRefreshSessions,
} from "./commands/openSessionFromTree";

let output: vscode.OutputChannel | undefined;
let controller: EditorPanelController | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel("Omnigent");
  context.subscriptions.push(output);
  output.appendLine("[omnigent] activating");

  // ── Single editor-beside iframe surface ───────────────────────────────────
  controller = new EditorPanelController(context.extensionUri, output);
  const panelController = controller;

  // ── Sessions tree view (the activity-bar surface) ──────────────────────────
  const sessionsProvider = new SessionsTreeProvider(
    () => panelController.getClientOpts(),
    output,
  );
  const treeView = vscode.window.createTreeView(SESSIONS_VIEW_ID, {
    treeDataProvider: sessionsProvider,
  });
  context.subscriptions.push(treeView);
  // Refresh the list whenever the tree becomes visible (cheap, no timer).
  context.subscriptions.push(
    treeView.onDidChangeVisibility((e) => {
      if (e.visible) void sessionsProvider.refresh();
    }),
  );

  // ── Commands ───────────────────────────────────────────────────────────────
  registerOpenPanel(context, controller);
  registerOpenSessionFromTree(context, controller, output);
  registerRefreshSessions(context, sessionsProvider);

  // ── Resolve the local server at activation ────────────────────────────────
  try {
    const settings = readSettings();
    const discovery = await discoverLocalServer(undefined, DEFAULT_HEALTH_TIMEOUT_MS);
    const resolution = resolveServerTarget(settings, {
      found: discovery.found,
      baseUrl: discovery.found ? discovery.baseUrl : undefined,
      health: discovery.found ? discovery.health : undefined,
    });

    if (resolution.status === "resolved") {
      const target = resolution.target;
      controller.setResolved(target);
      output.appendLine(
        `[omnigent] target: ${target.baseUrl} (hostType=${target.hostType}, source=${target.source})`,
      );
      // Now that a server target is known, populate the Sessions tree.
      void sessionsProvider.refresh();
    } else {
      output.appendLine(
        `[omnigent] no local server (${resolution.reason}); start \`omnigent server\` or set omnigent.serverUrl to a localhost URL`,
      );
    }
  } catch (err) {
    output.appendLine(
      `[omnigent] init error: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  output.appendLine("[omnigent] ready");
}

export function deactivate(): void {
  controller?.dispose();
  controller = undefined;
  output?.appendLine("[omnigent] deactivating");
  output = undefined;
}
