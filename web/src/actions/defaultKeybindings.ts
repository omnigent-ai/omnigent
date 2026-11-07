import { and, CONTEXT_KEYS, not, or, when } from "./context";
import { parseKeybinding } from "./keybindingParser";
import type { ActionArgs, ActionId, KeybindingMode, KeybindingRule } from "./types";

interface BaseRuleOptions {
  mode?: KeybindingMode;
  activation?: KeybindingRule["activation"];
  when?: KeybindingRule["when"];
  phase?: KeybindingRule["phase"];
  priority?: number;
  allowRepeat?: boolean;
  preventDefault?: boolean;
  stopPropagation?: boolean;
}

type RuleOptions<A extends ActionId> = BaseRuleOptions &
  (undefined extends ActionArgs<A> ? { args?: undefined } : { args: ActionArgs<A> });

function rule<A extends ActionId>(
  id: string,
  action: A,
  sequence: string,
  ...[options = {} as RuleOptions<A>]: undefined extends ActionArgs<A>
    ? [options?: RuleOptions<A>]
    : [options: RuleOptions<A>]
): KeybindingRule<A> {
  return {
    id,
    action,
    sequence: parseKeybinding(sequence),
    mode: options.mode ?? "global",
    activation: options.activation,
    when: options.when,
    args: options.args,
    phase: options.phase ?? "bubble",
    priority: options.priority,
    allowRepeat: options.allowRepeat,
    preventDefault: options.preventDefault ?? true,
    stopPropagation: options.stopPropagation ?? false,
  } as KeybindingRule<A>;
}

const suggestionsClosed = not(when(CONTEXT_KEYS.composerSuggestionsOpen));
const notCoarsePointer = not(when(CONTEXT_KEYS.isCoarsePointer));
const notEmbedded = not(when(CONTEXT_KEYS.isEmbedded));
const notNativeShell = not(when(CONTEXT_KEYS.isNativeShell));
const notInputFocus = not(when(CONTEXT_KEYS.inputFocus));
const notMonacoFocus = not(when(CONTEXT_KEYS.monacoFocus));
const notTerminalFocus = not(when(CONTEXT_KEYS.terminalFocus));
const paletteFocusAllowed = and(notMonacoFocus, or(notTerminalFocus, when(CONTEXT_KEYS.eventMeta)));
const dictationFocusAllowed = and(notMonacoFocus, notTerminalFocus);

const pinnedRules: KeybindingRule[] = [];
for (let slot = 0; slot < 10; slot += 1) {
  const digit = slot === 9 ? "0" : String(slot + 1);
  pinnedRules.push(
    rule(`session.openPinned.native.${digit}`, "session.action.openPinned", `primary+${digit}`, {
      args: { slot },
      when: when(CONTEXT_KEYS.isNativeShell),
      allowRepeat: true,
    }),
    rule(
      `session.openPinned.browser.${digit}`,
      "session.action.openPinned",
      `primary+alt+[Digit${digit}]`,
      {
        args: { slot },
        when: notNativeShell,
        allowRepeat: true,
      },
    ),
  );
}

/** The product defaults. User customizations are layered over these rules. */
export const DEFAULT_KEYBINDINGS: readonly KeybindingRule[] = [
  // New session is the one legacy action that requires the platform modifier
  // exactly; other migrated hooks historically accepted either Ctrl or Meta.
  rule("session.new", "session.action.new", "mod+n", { when: notEmbedded }),
  rule("workbench.showCommands", "workbench.action.showCommands", "primary+k", {
    phase: "capture",
    stopPropagation: true,
    when: paletteFocusAllowed,
  }),
  rule("workbench.openKeyboardShortcuts", "workbench.action.openKeyboardShortcuts", "primary+/"),
  rule(
    "workbench.toggleConversationsSidebar",
    "workbench.action.toggleConversationsSidebar",
    "primary+alt+[BracketLeft]",
    { stopPropagation: true },
  ),
  rule(
    "workbench.toggleWorkspaceSidebar",
    "workbench.action.toggleWorkspaceSidebar",
    "primary+alt+[BracketRight]",
    { stopPropagation: true },
  ),
  // Approval and send intentionally keep repeat disabled: a held Enter must
  // not accept multiple prompts or enqueue repeated messages.
  rule("chat.acceptApproval", "chat.action.acceptApproval", "primary+enter", {
    phase: "capture",
    stopPropagation: true,
  }),
  rule("session.openPrevious", "session.action.openPrevious", "primary+arrowup", {
    allowRepeat: true,
  }),
  rule("session.openNext", "session.action.openNext", "primary+arrowdown", {
    allowRepeat: true,
  }),
  ...pinnedRules,
  rule("chat.openPreviousMessage", "chat.action.openPreviousMessage", "primary+alt+arrowup", {
    allowRepeat: true,
  }),
  rule("chat.openNextMessage", "chat.action.openNextMessage", "primary+alt+arrowdown", {
    allowRepeat: true,
  }),
  rule("composer.toggleDictation", "composer.action.toggleDictation", "primary+alt+[KeyV]", {
    stopPropagation: true,
    when: dictationFocusAllowed,
  }),

  rule("composer.commitDictation", "composer.action.commitDictation", "enter", {
    mode: "composer",
    when: when(CONTEXT_KEYS.dictationListening),
    phase: "capture",
    priority: 200,
    stopPropagation: true,
  }),
  rule("composer.cancelDictation", "composer.action.cancelDictation", "escape", {
    mode: "composer",
    when: when(CONTEXT_KEYS.dictationListening),
    phase: "capture",
    priority: 200,
    stopPropagation: true,
  }),
  rule("composer.selectPreviousSuggestion", "composer.action.selectPreviousSuggestion", "arrowup", {
    mode: "composer",
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
    allowRepeat: true,
  }),
  rule("composer.selectNextSuggestion", "composer.action.selectNextSuggestion", "arrowdown", {
    mode: "composer",
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
    allowRepeat: true,
  }),
  rule("composer.acceptSuggestion.tab", "composer.action.acceptSuggestion", "tab", {
    mode: "composer",
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
    args: { behavior: "attach" },
  }),
  rule("composer.acceptSuggestion.enter", "composer.action.acceptSuggestion", "enter", {
    mode: "composer",
    when: and(when(CONTEXT_KEYS.composerSuggestionsOpen), notCoarsePointer),
    priority: 100,
    args: { behavior: "openOrAttach" },
  }),
  rule(
    "composer.acceptSuggestion.primaryEnter",
    "composer.action.acceptSuggestion",
    "primary+enter",
    {
      mode: "composer",
      when: and(when(CONTEXT_KEYS.composerSuggestionsOpen), notCoarsePointer),
      priority: 100,
      args: { behavior: "openOrAttach" },
    },
  ),
  rule("composer.dismissSuggestions", "composer.action.dismissSuggestions", "escape", {
    mode: "composer",
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
  }),
  rule("composer.send", "composer.action.send", "enter", {
    mode: "composer",
    when: and(suggestionsClosed, notCoarsePointer),
  }),
  rule("composer.send.primaryEnter", "composer.action.send", "primary+enter", {
    mode: "composer",
    when: and(suggestionsClosed, notCoarsePointer),
  }),
  rule("composer.stop", "composer.action.stop", "escape", {
    mode: "composer",
    when: and(suggestionsClosed, when(CONTEXT_KEYS.composerStreaming)),
  }),
  rule("composer.recallPrevious", "composer.action.recallPrevious", "arrowup", {
    mode: "composer",
    when: suggestionsClosed,
    allowRepeat: true,
  }),
  rule("composer.recallNext", "composer.action.recallNext", "arrowdown", {
    mode: "composer",
    when: suggestionsClosed,
    allowRepeat: true,
  }),

  rule("file.find", "file.action.find", "primary+f", { mode: "fileViewer" }),
  rule("file.save", "file.action.save", "primary+s", {
    mode: "fileViewer",
    phase: "capture",
  }),
  rule("file.selectAllContent", "file.action.selectAllContent", "primary+a", {
    mode: "fileViewer",
    when: and(when(CONTEXT_KEYS.shikiSourceView), notInputFocus),
  }),
  rule("file.openPreviousChanged", "file.action.openPreviousChanged", "alt+arrowleft", {
    mode: "fileViewer",
    when: notInputFocus,
    allowRepeat: true,
  }),
  rule("file.openNextChanged", "file.action.openNextChanged", "alt+arrowright", {
    mode: "fileViewer",
    when: notInputFocus,
    allowRepeat: true,
  }),
  rule("file.closeSearch", "file.action.closeSearch", "escape", {
    mode: "fileViewer",
    when: when(CONTEXT_KEYS.fileSearchOpen),
    priority: 100,
  }),
  rule("file.close", "file.action.close", "escape", {
    mode: "fileViewer",
    when: and(not(when(CONTEXT_KEYS.fileSearchOpen)), notInputFocus),
  }),

  rule("terminal.sendShiftEnter", "terminal.action.sendSequence", "shift+enter", {
    mode: "terminal",
    args: { data: "\u001b[13;2u" },
    phase: "capture",
    allowRepeat: true,
    stopPropagation: true,
  }),
  rule("panel.closeFiles", "panel.action.closeFiles", "escape", {
    mode: "filesPanel",
    activation: "active",
  }),
  rule("panel.closeTerminals", "panel.action.closeTerminals", "escape", {
    mode: "terminalsPanel",
    activation: "active",
  }),
  rule("panel.closeExecutionLogs", "panel.action.closeExecutionLogs", "escape", {
    mode: "executionLogs",
    activation: "active",
  }),
  rule("panel.closeMarkdownToc", "panel.action.closeMarkdownToc", "escape", {
    mode: "markdownToc",
    activation: "active",
  }),
];
