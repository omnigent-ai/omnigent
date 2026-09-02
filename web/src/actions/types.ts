export type JsonValue =
  null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export const ACTION_IDS = [
  "workbench.action.showCommands",
  "workbench.action.openKeyboardShortcuts",
  "workbench.action.navigateInbox",
  "workbench.action.navigateAutomations",
  "workbench.action.navigateSettings",
  "workbench.action.toggleConversationsSidebar",
  "workbench.action.toggleWorkspaceSidebar",
  "session.action.new",
  "session.action.openPrevious",
  "session.action.openNext",
  "session.action.openPinned",
  "chat.action.acceptApproval",
  "chat.action.openPreviousMessage",
  "chat.action.openNextMessage",
  "composer.action.send",
  "composer.action.stop",
  "composer.action.recallPrevious",
  "composer.action.recallNext",
  "composer.action.selectPreviousSuggestion",
  "composer.action.selectNextSuggestion",
  "composer.action.acceptSuggestion",
  "composer.action.dismissSuggestions",
  "composer.action.toggleDictation",
  "composer.action.commitDictation",
  "composer.action.cancelDictation",
  "file.action.find",
  "file.action.save",
  "file.action.selectAllContent",
  "file.action.openPreviousChanged",
  "file.action.openNextChanged",
  "file.action.closeSearch",
  "file.action.close",
  "terminal.action.sendSequence",
  "panel.action.closeFiles",
  "panel.action.closeTerminals",
  "panel.action.closeExecutionLogs",
  "panel.action.closeMarkdownToc",
] as const;

export type ActionId = (typeof ACTION_IDS)[number];

/** Compile-time payload for an action. Most actions take no arguments. */
export type ActionArgs<A extends ActionId> = A extends "session.action.openPinned"
  ? { slot: number }
  : A extends "composer.action.acceptSuggestion"
    ? { behavior: "openOrAttach" | "attach" }
    : A extends "terminal.action.sendSequence"
      ? { data: string }
      : undefined;

export type ArglessActionId = {
  [A in ActionId]: undefined extends ActionArgs<A> ? A : never;
}[ActionId];

export const ACTION_ICON_NAMES = [
  "CalendarClock",
  "Inbox",
  "Keyboard",
  "PanelLeft",
  "PanelRight",
  "Search",
  "Settings",
  "SquarePen",
] as const;
export type ActionIconName = (typeof ACTION_ICON_NAMES)[number];

type ActionArgsField<A extends ActionId> =
  undefined extends ActionArgs<A> ? { args?: undefined } : { args: ActionArgs<A> };

export type ActionCategory =
  "General" | "Navigation" | "Chat" | "Composer" | "Files" | "Terminal" | "View";

export interface ActionDefinition {
  id: ActionId;
  title: string;
  category: ActionCategory;
  description?: string;
  keywords?: readonly string[];
  /** Icon identifier interpreted by the UI; the catalog stays React-free. */
  icon?: ActionIconName;
  /** Whether the action is eligible for the command palette. */
  palette?: boolean;
  /** Stable command-palette order; lower values render first. */
  paletteOrder?: number;
}

export type ActionSource = "api" | "button" | "keyboard" | "menu" | "native" | "palette";

export type ActionInvocation<A extends ActionId = ActionId> = A extends ActionId
  ? {
      action: A;
      source: ActionSource;
      event?: KeyboardEvent;
    } & ActionArgsField<A>
  : never;

export type ActionResult = "handled" | "notHandled";

export const HANDLED = "handled" as const satisfies ActionResult;
export const NOT_HANDLED = "notHandled" as const satisfies ActionResult;

/**
 * Modes form an active scope ancestry: a focused nested editor can still match
 * fileViewer rules from its parent. `activation: "active"` also matches an
 * open scope outside the focused branch (used by panel-level Escape actions).
 */
export const KEYBINDING_MODES = [
  "global",
  "composer",
  "terminal",
  "codeEditor",
  "markdownEditor",
  "fileViewer",
  "commandPalette",
  "dialog",
  "filesPanel",
  "terminalsPanel",
  "executionLogs",
  "markdownToc",
] as const;

export type KeybindingMode = (typeof KEYBINDING_MODES)[number];

/**
 * `mod` is the conventional platform modifier (Meta on Apple, Ctrl elsewhere).
 * `primary` accepts either Meta or Ctrl to preserve legacy Omnigent shortcuts.
 */
export type KeyModifier = "mod" | "primary" | "ctrl" | "meta" | "alt" | "shift";

export interface KeyStroke {
  modifiers: readonly KeyModifier[];
  key: { kind: "key"; value: string } | { kind: "code"; value: string };
}

export type KeySequence = readonly KeyStroke[];

export interface ActionContextValues {
  isMac: boolean;
  isNativeShell: boolean;
  isElectron: boolean;
  isEmbedded: boolean;
  isCoarsePointer: boolean;
  inputFocus: boolean;
  terminalFocus: boolean;
  monacoFocus: boolean;
  eventMeta: boolean;
  composerStreaming: boolean;
  composerSuggestionsOpen: boolean;
  dictationListening: boolean;
  fileSearchOpen: boolean;
  shikiSourceView: boolean;
}

export type ContextKey = keyof ActionContextValues;
export type BooleanContextKey = {
  [K in ContextKey]: ActionContextValues[K] extends boolean ? K : never;
}[ContextKey];
export type ContextValue = ActionContextValues[ContextKey] | null | undefined;
export type ContextSnapshot = Readonly<ActionContextValues>;
export type ContextPatch = Readonly<Partial<ActionContextValues>>;

type EqualsExpression = {
  [K in ContextKey]: { type: "equals"; key: K; value: ActionContextValues[K] | null };
}[ContextKey];

export type ContextExpression =
  | { type: "truthy"; key: BooleanContextKey }
  | EqualsExpression
  | { type: "not"; expression: ContextExpression }
  | { type: "and"; expressions: readonly ContextExpression[] }
  | { type: "or"; expressions: readonly ContextExpression[] };

export type KeybindingRule<A extends ActionId = ActionId> = A extends ActionId
  ? {
      id: string;
      action: A;
      sequence: KeySequence;
      mode: KeybindingMode;
      /** Focused scopes win normally; active scopes model open global surfaces. */
      activation?: "focused" | "active";
      when?: ContextExpression;
      phase?: "capture" | "bubble";
      /** Higher values win among otherwise equally specific matching rules. */
      priority?: number;
      allowRepeat?: boolean;
      /** Preserve legacy globals that ran after a widget called preventDefault. */
      allowDefaultPrevented?: boolean;
      preventDefault?: boolean;
      stopPropagation?: boolean;
    } & ActionArgsField<A>
  : never;
