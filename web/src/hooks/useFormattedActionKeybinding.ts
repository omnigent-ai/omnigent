import {
  contextsMayOverlap,
  formatKeybinding,
  isMacKeyboardPlatform,
  keybindingEnvironmentExpression,
  useKeybindingSnapshot,
  type ActionId,
  type KeybindingMode,
} from "@/actions";
import { useIsEmbedded } from "@/lib/embedded";
import { isNativeShell } from "@/lib/nativeBridge";

/** First live effective binding for an action in the current runtime environment. */
export function useFormattedActionKeybinding(
  action: ActionId,
  options: { mode?: KeybindingMode } = {},
): string | null {
  const snapshot = useKeybindingSnapshot();
  const embedded = useIsEmbedded();
  const isMac = isMacKeyboardPlatform();
  const environment = keybindingEnvironmentExpression({
    isMac,
    isNativeShell: isNativeShell(),
    isEmbedded: embedded,
  });
  const rule = snapshot.effectiveRules.find(
    (candidate) =>
      candidate.action === action &&
      (!options.mode || candidate.mode === options.mode) &&
      contextsMayOverlap(candidate.when, environment),
  );
  return rule ? formatKeybinding(rule.sequence, { isMac }) : null;
}
