// Every IPC channel between the main process and its renderers, typed once (types
// only): what a preload sends or main pushes and, for requests, what main resolves.
// ipc/main.ts hands request arguments to handlers as `unknown`.

import type { DesktopCommand } from "../desktopCommands.ts";
import type {
  AboutInfo,
  ArcaConnectInit,
  ArcaConnectResult,
  ArcaConnectStarted,
  ArcaExtendMode,
  ArcaExtendResult,
  BrowserAction,
  BrowserCallResult,
  BrowserCloseResult,
  BrowserElementPromptDismiss,
  BrowserElementPromptSubmit,
  BrowserElementSelection,
  BrowserHostOptions,
  BrowserNavResult,
  BrowserNavState,
  BrowserOpenOptions,
  BrowserOpenResult,
  BrowserPermissionChoice,
  BrowserPermissionInfo,
  BrowserRecentSessionInput,
  BrowserScreenshotResult,
  BrowserScriptResult,
  BrowserUrlChange,
  BrowserViewBounds,
  BrowserViewState,
  ChromeCookieImportResult,
  ChromeCookiePreviewResult,
  CliStatus,
  ConsoleContentSize,
  ConsoleDone,
  DbcertRefreshOptions,
  DbcertRefreshResult,
  DbcertStatusResult,
  DesktopCommandStateReport,
  DesktopFeatures,
  FindResult,
  GlobalShortcutChange,
  GlobalShortcutSaveResult,
  GlobalShortcutStatus,
  HostActionResult,
  HostControlAction,
  HostIdentity,
  OnboardingRunner,
  ResolvedTheme,
  ServerPickerInfo,
  SetupActionResult,
  SetupCliStatus,
  SshConnectAnswer,
  SshConnectInit,
  SshConnectPrompt,
  SshConnectResult,
  SshConnectStatus,
  ThemeSource,
  UpdateConfig,
  UpdateStatus,
  WorkspaceChoice,
} from "./bridge.ts";

/** `{ conversationId }`, the argument of most embedded-browser requests. */
interface ConversationRequest {
  conversationId: string;
}

/** A line of streamed setup output. */
interface LogLine {
  line: string;
}

/** Renderer → main requests (`ipcRenderer.invoke` / `ipcMain.handle`), by channel. */
export interface InvokeChannels {
  // Main window: window state and native integrations.
  "omnigent:is-full-screen": { args: []; result: boolean };
  "omnigent:notify": {
    args: [params: { title: string; body: string | undefined; navigatePath: string | undefined }];
    result: boolean;
  };
  "omnigent:close-window": { args: []; result: boolean };
  "omnigent:confirm-quit": { args: [options: { dontAskAgain: boolean }]; result: boolean };
  "omnigent:start-system-dictation": { args: []; result: boolean };
  "omnigent:get-global-shortcut": { args: []; result: GlobalShortcutStatus };
  "omnigent:set-global-shortcut": {
    args: [change: GlobalShortcutChange];
    result: GlobalShortcutSaveResult;
  };
  "omnigent:record-global-shortcut": { args: []; result: string | null };
  "omnigent:cancel-global-shortcut-recording": { args: []; result: boolean };
  "omnigent:take-onboarding-runner": { args: []; result: OnboardingRunner | null };
  "omnigent:get-server-picker": { args: []; result: ServerPickerInfo | null };
  "omnigent:switch-server": { args: [url: string]; result: void };
  "omnigent:sign-out-of-server": { args: []; result: boolean };
  "omnigent:reveal-file": { args: [hostId: string, path: string]; result: boolean };
  "omnigent:choose-directory": { args: [defaultPath: string | undefined]; result: string | null };
  "omnigent:dropped-file-path": { args: [filePath: string]; result: string };
  "omnigent:host-get-identity": { args: []; result: HostIdentity | null };
  "omnigent:host-control": { args: [action: HostControlAction]; result: HostActionResult };
  "omnigent:get-desktop-features": { args: []; result: DesktopFeatures | null };
  "omnigent:arca-connect": { args: []; result: ArcaConnectResult };
  "omnigent:ssh-connect": { args: [destination: string]; result: SshConnectResult };
  "omnigent:arca-extend": { args: [mode: ArcaExtendMode]; result: ArcaExtendResult };
  "omnigent:dbcert-refresh": {
    args: [options: DbcertRefreshOptions | undefined];
    result: DbcertRefreshResult;
  };
  "omnigent:dbcert-check": { args: []; result: DbcertStatusResult };
  "omnigent:cli-get-status": { args: []; result: CliStatus | null };
  "omnigent:cli-reset-path": { args: []; result: CliStatus | null };

  // Main window: update preferences (the server page's update bridge).
  "omnigent:get-update-config": { args: []; result: UpdateConfig };
  "omnigent:get-update-status": { args: []; result: UpdateStatus };
  "omnigent:update-check": { args: []; result: void };
  "omnigent:update-download": { args: []; result: void };
  "omnigent:update-install": { args: []; result: void };
  "omnigent:set-update-config": { args: [patch: Partial<UpdateConfig>]; result: UpdateConfig };
  "omnigent:get-update-overlay-height": { args: []; result: number };

  // Main window: embedded browser views.
  "omnigent:browser-open-or-navigate": {
    args: [
      request: {
        conversationId: string;
        url: string;
        bounds: BrowserViewBounds | undefined;
        opts: BrowserOpenOptions | undefined;
      },
    ];
    result: BrowserOpenResult;
  };
  "omnigent:browser-set-active": {
    args: [request: { conversationId: string | null; focus: boolean }];
    result: BrowserCallResult;
  };
  "omnigent:browser-focused-view": { args: []; result: string | null };
  "omnigent:browser-set-suppressed": {
    args: [request: { suppressed: boolean }];
    result: BrowserCallResult;
  };
  "omnigent:browser-resize": {
    args: [request: { conversationId: string; bounds: BrowserViewBounds }];
    result: BrowserCallResult;
  };
  "omnigent:browser-screenshot": {
    args: [request: { conversationId: string; opts: BrowserHostOptions | undefined }];
    result: BrowserScreenshotResult;
  };
  "omnigent:browser-perform-action": {
    args: [
      request: {
        conversationId: string;
        action: BrowserAction;
        params: Record<string, unknown> | undefined;
        opts: BrowserHostOptions | undefined;
      },
    ];
    result: BrowserScriptResult;
  };
  "omnigent:browser-execute": {
    args: [request: { conversationId: string; js: string; opts: BrowserHostOptions | undefined }];
    result: BrowserScriptResult;
  };
  "omnigent:browser-close": {
    args: [request: { conversationId: string; reason: string | undefined }];
    result: BrowserCloseResult;
  };
  "omnigent:browser-set-recent-session-switch-supported": {
    args: [request: { supported: boolean }];
    result: BrowserCallResult;
  };
  "omnigent:browser-set-open-tab-supported": {
    args: [request: { supported: boolean }];
    result: BrowserCallResult;
  };
  "omnigent:browser-cancel-recent-session-switch": { args: []; result: BrowserCallResult };
  "omnigent:browser-has-view": { args: [request: ConversationRequest]; result: BrowserViewState };
  "omnigent:browser-go-back": { args: [request: ConversationRequest]; result: BrowserNavResult };
  "omnigent:browser-go-forward": { args: [request: ConversationRequest]; result: BrowserNavResult };
  "omnigent:browser-reload": { args: [request: ConversationRequest]; result: BrowserCallResult };
  "omnigent:open-browser-devtools": {
    args: [request: ConversationRequest];
    result: BrowserCallResult;
  };
  "omnigent:browser-chrome-cookie-preview": {
    args: [request: ConversationRequest];
    result: ChromeCookiePreviewResult;
  };
  "omnigent:browser-chrome-cookie-import": {
    args: [
      request: {
        conversationId: string;
        profileId: string;
        expectedHost: string;
        scope: string | undefined;
      },
    ];
    result: ChromeCookieImportResult;
  };
  "omnigent:browser-enable-design-mode": {
    args: [request: ConversationRequest];
    result: BrowserCallResult;
  };
  "omnigent:browser-disable-design-mode": {
    args: [request: ConversationRequest];
    result: BrowserCallResult;
  };
  "omnigent:browser-signal-design-result": {
    args: [
      request: { conversationId: string; id: number; ok: boolean; message: string | undefined },
    ];
    result: BrowserCallResult;
  };

  // Setup page.
  "omnigent:get-server-url": { args: []; result: string | null };
  "omnigent:set-server-url": {
    args: [url: string, opts: { requestId?: string } | undefined];
    result: { cancelled?: true; reconnecting?: true };
  };
  "omnigent:cancel-server-connection": { args: [requestId: string]; result: boolean };
  "omnigent:get-managed-servers": { args: []; result: string[] };
  "omnigent:get-managed-server-names": { args: []; result: Record<string, string> };
  "omnigent:get-server-names": { args: []; result: Record<string, string> };
  "omnigent:get-setup-capabilities": {
    args: [];
    result: { v2Forced: boolean; connectedBefore: boolean };
  };
  "omnigent:get-runner-options": {
    args: [url: string];
    result: { remote: boolean; bundledCli: boolean };
  };
  "omnigent:connect-runner": {
    args: [url: string, runner: OnboardingRunner];
    result: SetupActionResult & { canceled?: boolean };
  };
  "omnigent:setup-get-color-scheme": {
    args: [];
    result: { source: ThemeSource; effective: ResolvedTheme } | null;
  };
  "omnigent:get-recent-servers": { args: []; result: string[] };
  "omnigent:forget-recent-server": { args: [url: string]; result: string[] };
  "omnigent:check-server": {
    args: [url: string];
    result: { status: "ok" | "reachable" | "unreachable" };
  };
  "omnigent:copy-setup-text": { args: [text: string]; result: void };
  "omnigent:set-server-selector-v2": { args: [enabled: boolean]; result: void };
  "omnigent:get-cli-status": { args: []; result: SetupCliStatus };
  "omnigent:set-cli-path": { args: [path: string]; result: CliStatus };
  "omnigent:browse-cli-path": { args: []; result: string | null };
  "omnigent:start-local-server": {
    args: [];
    result: SetupActionResult & { url?: string; alreadyRunning?: boolean };
  };
  "omnigent:cli-install": { args: []; result: SetupActionResult & { installed: boolean } };

  // About window.
  "omnigent:about-get-info": { args: []; result: AboutInfo };
  "omnigent:about-get-desktop-update-status": { args: []; result: UpdateStatus };
  "omnigent:about-check-desktop": { args: []; result: UpdateStatus };
  "omnigent:about-download-desktop-update": { args: []; result: void };
  "omnigent:about-install-desktop-update": { args: []; result: void };

  // Update overlay.
  "omnigent:overlay-get-update-config": { args: []; result: UpdateConfig };
  "omnigent:overlay-get-update-status": { args: []; result: UpdateStatus };
  "omnigent:overlay-update-check": { args: []; result: void };
  "omnigent:overlay-update-download": { args: []; result: void };
  "omnigent:overlay-update-install": { args: []; result: void };
  "omnigent:overlay-set-update-config": {
    args: [patch: Partial<UpdateConfig>];
    result: UpdateConfig;
  };

  // Browser permission prompt.
  "omnigent:browser-permission-info": { args: []; result: BrowserPermissionInfo };
  "omnigent:browser-permission-choose": { args: [choice: BrowserPermissionChoice]; result: void };

  // Workspace picker.
  "workspacePicker:list": { args: []; result: WorkspaceChoice[] };
}

/** Renderer → main one-way messages, by channel. */
export interface SendChannels {
  // Main window.
  "omnigent:set-badge-count": { args: [count: number] };
  "omnigent:set-desktop-command-state": { args: [state: DesktopCommandStateReport] };
  "omnigent:open-server-setup": { args: [] };
  "omnigent:set-color-scheme": { args: [scheme: ThemeSource] };

  // Setup page.
  "omnigent:setup-set-color-scheme": { args: [scheme: ThemeSource] };

  // About window.
  "omnigent:about-close": { args: [] };

  // Find bar.
  "omnigent:find-query": { args: [query: { text: string; forward: boolean; findNext: boolean }] };
  "omnigent:find-close": { args: [] };

  // Overlays and banners.
  "omnigent:reconnect-overlay-cancel": { args: [] };
  "omnigent:return-banner-go-back": { args: [] };
  "omnigent:return-banner-dismiss": { args: [] };
  "omnigent:overlay-height": { args: [height: number] };

  // Workspace picker.
  "workspacePicker:choose": { args: [workspaceId: string] };
  "workspacePicker:cancel": { args: [] };

  // Arca connect console.
  "arca-connect:confirm": { args: [] };
  "arca-connect:cancel": { args: [] };
  "arca-connect:resize": { args: [size: ConsoleContentSize] };

  // SSH connect console.
  "ssh-connect:confirm": { args: [] };
  "ssh-connect:cancel": { args: [] };
  "ssh-connect:answer": { args: [answer: SshConnectAnswer] };
  "ssh-connect:resize": { args: [size: ConsoleContentSize] };
}

/** A native menu command as main sends it; the preload fills in missing fields. */
export interface DesktopCommandPush {
  command: DesktopCommand;
  browserViewId?: string | null;
  closingQuits?: boolean;
  quitDetail?: string | null;
}

/** Main → renderer pushes (`webContents.send` / `ipcRenderer.on`), by channel. */
export interface PushChannels {
  // Main window.
  "omnigent:full-screen-changed": { args: [fullScreen: boolean] };
  "omnigent:notification-activated": { args: [path: string] };
  "omnigent:open-path": { args: [path: string] };
  "omnigent:find-opened": { args: [] };
  "omnigent:desktop-command": { args: [message: DesktopCommandPush] };
  "omnigent:host-status-changed": { args: [] };
  "omnigent:update-status": { args: [status: UpdateStatus] };
  "omnigent:update-overlay-height": { args: [height: number] };
  "browser-host-active-changed": { args: [payload: { conversationId: string | null }] };
  "browser-recent-session-input": { args: [input: BrowserRecentSessionInput] };
  "browser-open-tab": { args: [payload: { conversationId: string; url: string }] };
  "browser-view-created": { args: [payload: { conversationId: string }] };
  "browser-view-closed": { args: [payload: { conversationId: string; reason: string | null }] };
  "browser-url-changed": { args: [payload: BrowserUrlChange] };
  "browser-nav-state": { args: [payload: BrowserNavState] };
  /** Pushed by main; no preload listens for it. */
  "browser-nav-blocked": {
    args: [payload: { conversationId: string; url: string; error: string }];
  };
  "browser-element-selected": { args: [payload: BrowserElementSelection] };
  "browser-element-prompt-submit": { args: [payload: BrowserElementPromptSubmit] };
  "browser-element-prompt-dismiss": { args: [payload: BrowserElementPromptDismiss] };

  // Setup page.
  "omnigent:connection-progress": {
    args: [progress: { requestId?: string; phase: "connecting" | "authenticating" }];
  };
  "omnigent:runner-connect-log": { args: [payload: LogLine] };
  "omnigent:setup-theme": { args: [theme: ResolvedTheme] };
  "omnigent:local-server-setup-log": { args: [payload: LogLine] };
  "omnigent:cli-install-log": { args: [payload: LogLine] };

  // Connection loading indicator.
  "omnigent:connection-loading": { args: [label: string] };

  // Find bar.
  "omnigent:find-result": { args: [result: FindResult] };
  "omnigent:find-activate": { args: [] };

  // Update overlay.
  "omnigent:overlay-theme": { args: [theme: ResolvedTheme] };

  // Arca connect console.
  "arca-connect:init": { args: [init: ArcaConnectInit] };
  "arca-connect:started": { args: [started: ArcaConnectStarted] };
  "arca-connect:output": { args: [text: string] };
  "arca-connect:done": { args: [done: ConsoleDone] };

  // SSH connect console.
  "ssh-connect:init": { args: [init: SshConnectInit] };
  "ssh-connect:started": { args: [started: Record<string, never>] };
  "ssh-connect:status": { args: [status: SshConnectStatus] };
  "ssh-connect:prompt": { args: [prompt: SshConnectPrompt] };
  "ssh-connect:prompt-closed": { args: [closed: { id: number }] };
  "ssh-connect:output": { args: [text: string] };
  "ssh-connect:done": { args: [done: ConsoleDone] };
}
