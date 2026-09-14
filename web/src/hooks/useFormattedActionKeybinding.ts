import {
  and,
  contextsMayOverlap,
  formatKeybinding,
  isMacKeyboardPlatform,
  keybindingEnvironmentExpression,
  useKeybindingSnapshot,
  type ActionId,
  type ContextExpression,
  type KeybindingMode,
} from "@/actions";
import { useIsEmbedded } from "@/lib/embedded";
import { isNativeShell } from "@/lib/nativeBridge";

/** First live effective binding for an action in the current runtime environment. */
export function useFormattedActionKeybinding(
  action: ActionId,
  options: { mode?: KeybindingMode; context?: ContextExpression } = {},
): string | null {
  const snapshot = useKeybindingSnapshot();
  const embedded = useIsEmbedded();
  const isMac = isMacKeyboardPlatform();
  const platformEnvironment = keybindingEnvironmentExpression({
    isMac,
    isNativeShell: isNativeShell(),
    isEmbedded: embedded,
  });
  const environment = options.context
    ? and(platformEnvironment, options.context)
    : platformEnvironment;
  const rule = snapshot.effectiveRules.find(
    (candidate) =>
      candidate.action === action &&
      (!options.mode || candidate.mode === options.mode) &&
      contextsMayOverlap(candidate.when, environment),
  );
  return rule ? formatKeybinding(rule.sequence, { isMac }) : null;
}
