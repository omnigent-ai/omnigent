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
  allowDefaultPrevented?: boolean;
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
    allowDefaultPrevented: options.allowDefaultPrevented,
    preventDefault: options.preventDefault ?? true,
    stopPropagation: options.stopPropagation ?? false,
  } as KeybindingRule<A>;
}

// prompt-toolkit maps CSI-u Shift+Enter to F20; ESC+CR is the old Alt+Enter fallback.
const SHIFT_ENTER_CSI_U = "\u001b[13;2u";

const suggestionsClosed = not(when(CONTEXT_KEYS.composerSuggestionsOpen));
const composerEnterSends = not(when(CONTEXT_KEYS.composerEnterInserts));
const composerSubmitWithModEnter = when(CONTEXT_KEYS.composerSubmitWithModEnter);
const composerSubmitWithPlainEnter = not(composerSubmitWithModEnter);
const notEmbedded = not(when(CONTEXT_KEYS.isEmbedded));
const notNativeShell = not(when(CONTEXT_KEYS.isNativeShell));
const notInputFocus = not(when(CONTEXT_KEYS.inputFocus));
const notMonacoFocus = not(when(CONTEXT_KEYS.monacoFocus));
const notTerminalFocus = not(when(CONTEXT_KEYS.terminalFocus));
const paletteFocusAllowed = and(notMonacoFocus, or(notTerminalFocus, when(CONTEXT_KEYS.eventMeta)));
const dictationFocusAllowed = and(notMonacoFocus, notTerminalFocus);
const fileCommandFocusAllowed = or(
  notInputFocus,
  when(CONTEXT_KEYS.monacoFocus),
  when(CONTEXT_KEYS.markdownEditorFocus),
);

// Native shells own plain primary+digit. Browser tabs reserve that chord, so
// web adds Alt and matches physical DigitN (Alt rewrites e.key on macOS).
// Legacy pinned handlers also ran after preventDefault, so these rules opt out
// of the dispatcher's normal focused-widget handoff.
const pinnedRules: KeybindingRule[] = [];
for (let slot = 0; slot < 10; slot += 1) {
  const digit = slot === 9 ? "0" : String(slot + 1);
  pinnedRules.push(
    rule(`session.openPinned.native.${digit}`, "session.action.openPinned", `primary+${digit}`, {
      args: { slot },
      when: when(CONTEXT_KEYS.isNativeShell),
      allowRepeat: true,
      allowDefaultPrevented: true,
    }),
    rule(
      `session.openPinned.browser.${digit}`,
      "session.action.openPinned",
      `primary+alt+[Digit${digit}]`,
      {
        args: { slot },
        when: notNativeShell,
        allowRepeat: true,
        allowDefaultPrevented: true,
      },
    ),
  );
}

/** The product defaults. User customizations are layered over these rules. */
export const DEFAULT_KEYBINDINGS: readonly KeybindingRule[] = [
  // New session is the one legacy action that requires the platform modifier
  // exactly; other migrated hooks historically accepted either Ctrl or Meta.
  rule("session.new", "session.action.new", "mod+n", {
    when: notEmbedded,
    allowDefaultPrevented: true,
    stopPropagation: true,
  }),
  rule("workbench.showCommands", "workbench.action.showCommands", "primary+k", {
    phase: "capture",
    stopPropagation: true,
    when: paletteFocusAllowed,
  }),
  rule("workbench.openKeyboardShortcuts", "workbench.action.openKeyboardShortcuts", "primary+/", {
    allowDefaultPrevented: true,
  }),
  rule(
    "workbench.toggleConversationsSidebar",
    "workbench.action.toggleConversationsSidebar",
    "primary+alt+[BracketLeft]",
    { allowDefaultPrevented: true, stopPropagation: true },
  ),
  rule(
    "workbench.toggleWorkspaceSidebar",
    "workbench.action.toggleWorkspaceSidebar",
    "primary+alt+[BracketRight]",
    { allowDefaultPrevented: true, stopPropagation: true },
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
  // The legacy message navigator accepted Shift as an extra modifier.
  rule(
    "chat.openPreviousMessage.shift",
    "chat.action.openPreviousMessage",
    "primary+alt+shift+arrowup",
    { allowRepeat: true },
  ),
  rule("chat.openNextMessage.shift", "chat.action.openNextMessage", "primary+alt+shift+arrowdown", {
    allowRepeat: true,
  }),
  // Physical KeyV survives Option-modified layouts; terminal and Monaco own
  // their focused keys, while other legacy widgets did not block this global.
  rule("composer.toggleDictation", "composer.action.toggleDictation", "primary+alt+[KeyV]", {
    stopPropagation: true,
    allowDefaultPrevented: true,
    when: dictationFocusAllowed,
  }),

  // Listening handlers are globally registered like the legacy capture
  // listener; their live isEnabled gate makes these inert between takes.
  rule("composer.commitDictation", "composer.action.commitDictation", "enter", {
    phase: "capture",
    priority: 200,
    stopPropagation: true,
  }),
  rule("composer.cancelDictation", "composer.action.cancelDictation", "escape", {
    phase: "capture",
    priority: 200,
    stopPropagation: true,
  }),
  rule("composer.cancelDictation.shift", "composer.action.cancelDictation", "shift+escape", {
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
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
    args: { behavior: "openOrAttach" },
  }),
  rule(
    "composer.acceptSuggestion.primaryEnter",
    "composer.action.acceptSuggestion",
    "primary+enter",
    {
      mode: "composer",
      when: and(when(CONTEXT_KEYS.composerSuggestionsOpen), composerSubmitWithPlainEnter),
      priority: 100,
      args: { behavior: "openOrAttach" },
    },
  ),
  rule("composer.acceptSuggestion.altEnter", "composer.action.acceptSuggestion", "alt+enter", {
    mode: "composer",
    when: when(CONTEXT_KEYS.composerSuggestionsOpen),
    priority: 100,
    args: { behavior: "openOrAttach" },
  }),
  rule(
    "composer.acceptSuggestion.primaryAltEnter",
    "composer.action.acceptSuggestion",
    "primary+alt+enter",
    {
      mode: "composer",
      when: when(CONTEXT_KEYS.composerSuggestionsOpen),
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
    when: and(suggestionsClosed, composerEnterSends, composerSubmitWithPlainEnter),
  }),
  rule("composer.send.primaryEnter", "composer.action.send", "primary+enter", {
    mode: "composer",
    when: and(composerEnterSends, or(suggestionsClosed, composerSubmitWithModEnter)),
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

  rule("file.find", "file.action.find", "primary+f", {
    mode: "fileViewer",
    activation: "active",
    when: fileCommandFocusAllowed,
    phase: "capture",
    stopPropagation: true,
  }),
  rule("file.save", "file.action.save", "primary+s", {
    mode: "fileViewer",
    activation: "active",
    when: fileCommandFocusAllowed,
    phase: "capture",
    stopPropagation: true,
  }),
  rule("file.selectAllContent", "file.action.selectAllContent", "primary+a", {
    mode: "fileViewer",
    activation: "active",
    when: notInputFocus,
  }),
  rule("file.openPreviousChanged", "file.action.openPreviousChanged", "alt+arrowleft", {
    mode: "fileViewer",
    activation: "active",
    when: notInputFocus,
    allowRepeat: true,
  }),
  rule("file.openNextChanged", "file.action.openNextChanged", "alt+arrowright", {
    mode: "fileViewer",
    activation: "active",
    when: notInputFocus,
    allowRepeat: true,
  }),
  rule("file.closeSearch", "file.action.closeSearch", "escape", {
    mode: "fileViewer",
    activation: "active",
    when: when(CONTEXT_KEYS.fileSearchOpen),
    priority: 100,
  }),
  rule("file.close", "file.action.close", "escape", {
    mode: "fileViewer",
    activation: "active",
    when: and(not(when(CONTEXT_KEYS.fileSearchOpen)), notInputFocus),
  }),

  rule("terminal.sendShiftEnter", "terminal.action.sendSequence", "shift+enter", {
    mode: "terminal",
    args: { data: SHIFT_ENTER_CSI_U },
    when: when(CONTEXT_KEYS.terminalFocus),
    phase: "capture",
    allowRepeat: true,
    stopPropagation: true,
  }),
  // Active panel ties intentionally resolve by this bottom-to-top layering order.
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
