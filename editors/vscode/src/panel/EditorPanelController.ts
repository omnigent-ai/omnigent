/**
 * Sole owner of the editor-beside Omnigent `WebviewPanel` AND the resolved
 * server target that drives its render.
 *
 * The panel hosts the running LOCAL Omnigent server in a single static <iframe>
 * (panel/host.ts). There is no in-panel routing or messaging: the framed app
 * owns its own navigation. This is the ONLY `createWebviewPanel` call in the
 * codebase.
 */
import * as vscode from "vscode";
import { renderInto, renderResolvingHtml } from "./host";
import type { ServerTarget } from "../config";

export class EditorPanelController {
  private panel?: vscode.WebviewPanel;
  private resolved?: { target: ServerTarget };

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly output: vscode.OutputChannel,
  ) {}

  /**
   * Store the resolved server target. Called from extension.ts once the local
   * server resolves. If a panel is already open (e.g. opened during the async
   * discovery window), re-render it so it leaves the "Resolving…" placeholder.
   */
  setResolved(target: ServerTarget): void {
    this.resolved = { target };
    if (this.panel) {
      this.render(this.panel.webview);
    }
  }

  /** Create-or-reveal the editor-beside panel and render it. */
  ensure(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside);
      this.render(this.panel.webview);
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "omnigent",
      "Omnigent",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      },
    );
    this.panel = panel;
    panel.onDidDispose(() => {
      // Guard against a fire after the controller already dropped this panel.
      if (this.panel === panel) {
        this.panel = undefined;
      }
    });
    this.render(panel.webview);
    this.output.appendLine("[omnigent] opened editor-beside panel");
  }

  /** Whether the editor panel is currently open. */
  isOpen(): boolean {
    return this.panel !== undefined;
  }

  /** Dispose the panel and null the ref (idempotent). */
  dispose(): void {
    const panel = this.panel;
    this.panel = undefined;
    panel?.dispose();
  }

  private render(webview: vscode.Webview): void {
    if (!this.resolved) {
      // No resolved target yet — show the placeholder until setResolved arrives.
      webview.html = renderResolvingHtml();
      return;
    }
    renderInto(webview, {
      target: this.resolved.target,
      log: (m) => this.output.appendLine(m),
    });
  }
}
