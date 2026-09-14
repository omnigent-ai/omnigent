// Compact, read-only reference for the live effective keymap. Editing lives in Settings.
import { useMemo, useState } from "react";
import {
  ACTION_CATALOG,
  HANDLED,
  and,
  contextsMayOverlap,
  equals,
  isMacKeyboardPlatform,
  isReservedEscapeSequence,
  keybindingEnvironmentExpression,
  useKeybindingSnapshot,
  useRegisterAction,
  type ActionId,
  type KeybindingRule,
} from "@/actions";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { useIsEmbedded } from "@/lib/embedded";
import { isNativeShell } from "@/lib/nativeBridge";
import { KEYBINDING_MODE_LABELS, KeybindingSequence } from "./keybindings/KeybindingSequence";

function useCompactEffectiveBindings() {
  const snapshot = useKeybindingSnapshot();
  const native = isNativeShell();
  const embedded = useIsEmbedded();
  const isMac = isMacKeyboardPlatform();
  const submitWithModEnter = readSubmitWithModEnter();
  return useMemo(() => {
    const environment = keybindingEnvironmentExpression({
      isMac,
      isNativeShell: native,
      isEmbedded: embedded,
    });
    const composerSendEnvironment = and(
      environment,
      equals("composerSuggestionsOpen", false),
      equals("composerEnterInserts", false),
      equals("composerSubmitWithModEnter", submitWithModEnter),
    );
    const firstByAction = new Map<ActionId, KeybindingRule>();
    const customizedActions = new Set<ActionId>();
    for (const rule of snapshot.effectiveRules) {
      const ruleEnvironment =
        rule.action === "composer.action.send" ? composerSendEnvironment : environment;
      if (
        isReservedEscapeSequence(rule.sequence) ||
        !contextsMayOverlap(rule.when, ruleEnvironment)
      )
        continue;
      if (!firstByAction.has(rule.action)) firstByAction.set(rule.action, rule);
      if (rule.origin === "user") customizedActions.add(rule.action);
    }
    return ACTION_CATALOG.flatMap((definition) => {
      const rule = firstByAction.get(definition.id);
      if (!rule || (!definition.shortcutReference && !customizedActions.has(definition.id))) {
        return [];
      }
      return [{ definition, rule }];
    });
  }, [embedded, isMac, native, snapshot.effectiveRules, submitWithModEnter]);
}

/** Effective shortcut reference grouped by catalog category. */
export function KeyboardShortcutsList() {
  const bindings = useCompactEffectiveBindings();
  const groups = useMemo(() => {
    const grouped = new Map<string, typeof bindings>();
    for (const binding of bindings) {
      grouped.set(binding.definition.category, [
        ...(grouped.get(binding.definition.category) ?? []),
        binding,
      ]);
    }
    return [...grouped];
  }, [bindings]);
  return (
    <>
      {groups.map(([category, rows]) => (
        <section key={category} className="mb-4 last:mb-0">
          <h3 className="mb-1 text-sm font-medium text-muted-foreground">{category}</h3>
          <ul>
            {rows.map(({ definition, rule }) => (
              <li
                key={definition.id}
                data-action-id={definition.id}
                className="flex items-center justify-between gap-4 border-b border-border/60 py-2.5 last:border-b-0"
              >
                <span className="min-w-0 text-ui text-foreground">
                  {definition.title}
                  {rule.action === "session.action.openPinned" && (
                    <span className="text-muted-foreground"> · Slots 1–10</span>
                  )}
                  {rule.mode !== "global" && (
                    <span className="ml-1.5 text-xs text-muted-foreground">
                      {KEYBINDING_MODE_LABELS[rule.mode]}
                    </span>
                  )}
                </span>
                <KeybindingSequence sequence={rule.sequence} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </>
  );
}

export function KeyboardShortcutsDialog() {
  const [open, setOpen] = useState(false);

  useRegisterAction("workbench.action.openKeyboardShortcuts", {
    acceptsKeybindings: true,
    run: ({ source }) => {
      setOpen((previous) => (source === "keyboard" ? !previous : true));
      return HANDLED;
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription className="sr-only">
            The active keyboard shortcuts available in the application.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-y-auto pr-1">
          <KeyboardShortcutsList />
        </div>
      </DialogContent>
    </Dialog>
  );
}
