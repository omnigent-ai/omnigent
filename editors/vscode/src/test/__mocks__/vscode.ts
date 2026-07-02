/**
 * Minimal vscode API stub for vitest unit tests.
 * Only the symbols actually imported by the modules under test need to be here.
 * The pure logic (csp, iframeHtml, host helpers, discovery, config) never calls
 * into vscode — only the thin adapters and EditorPanelController do, and the
 * controller test injects a fake panel via createWebviewPanel. This stub exists
 * mainly to satisfy the module resolver.
 */

export const window = {
  createOutputChannel: () => ({ appendLine: () => {}, dispose: () => {} }),
  showInformationMessage: async () => undefined,
  showWarningMessage: async () => undefined,
  showErrorMessage: async () => undefined,
  registerTreeDataProvider: (_id: string, _provider: unknown) => ({ dispose: () => {} }),
  // Default stub panel; the controller test overrides this with a fake panel.
  createWebviewPanel: (_id: string, _title: string, _col: unknown, _opts: unknown) => ({
    webview: { html: "", postMessage: () => true, cspSource: "vscode-resource:" },
    reveal: () => {},
    onDidDispose: (_cb: () => void) => ({ dispose: () => {} }),
    dispose: () => {},
  }),
  activeColorTheme: { kind: 2 /* Dark */ },
};

export const workspace = {
  getConfiguration: () => ({ get: (_key: string, def: unknown) => def }),
};

export const commands = {
  registerCommand: (_id: string, _fn: unknown) => ({ dispose: () => {} }),
  executeCommand: async () => undefined,
};

export const Uri = {
  parse: (s: string) => ({ toString: () => s, fsPath: s }),
  joinPath: (base: { fsPath: string }, ...parts: string[]) => ({
    toString: () => [base.fsPath, ...parts].join("/"),
    fsPath: [base.fsPath, ...parts].join("/"),
  }),
};

export const ViewColumn = { Active: -1, Beside: -2, One: 1, Two: 2 };
export const ColorThemeKind = { Light: 1, Dark: 2, HighContrast: 3, HighContrastLight: 4 };

export const TreeItem = class {
  label: string;
  collapsibleState: number;
  id?: string;
  description?: string;
  tooltip?: unknown;
  iconPath?: unknown;
  contextValue?: string;
  command?: unknown;
  constructor(label: string, collapsibleState = 0) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
};

export const TreeItemCollapsibleState = { None: 0, Collapsed: 1, Expanded: 2 };

export const ThemeIcon = class {
  id: string;
  constructor(id: string) {
    this.id = id;
  }
};

export const MarkdownString = class {
  value: string;
  constructor(value = "") {
    this.value = value;
  }
};

export const EventEmitter = class {
  private listeners: Array<(e: unknown) => void> = [];
  event = (listener: (e: unknown) => void) => {
    this.listeners.push(listener);
    return { dispose: () => {} };
  };
  fire = (e?: unknown) => {
    for (const l of this.listeners) l(e);
  };
  dispose = () => {
    this.listeners = [];
  };
};
