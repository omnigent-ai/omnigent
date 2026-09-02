export { ACTION_CATALOG, ACTIONS_BY_ID, getActionDefinition } from "./catalog";
export {
  ActionScope,
  ActionScopeProvider,
  ActionsProvider,
  useActionAvailable,
  useActionScopeRegistration,
  useActions,
  useAvailableActions,
  usePaletteActions,
  useSuspendKeybindingDispatch,
} from "./ActionProvider";
export type { ActionScopeHandle, ActionScopeOptions, ActionsApi } from "./ActionProvider";
export { DesktopActionSync } from "./DesktopActionSync";
export { KeybindingDispatcher } from "./KeybindingDispatcher";
export { DEFAULT_KEYBINDINGS } from "./defaultKeybindings";
export {
  getKeybindingSnapshot,
  replaceAllUserKeybindings,
  resetAllUserKeybindings,
  resetUserKeybindingRule,
  setUserKeybindingCandidate,
  setUserKeybindingRule,
  unbindDefaultKeybinding,
  useKeybindingSnapshot,
} from "./KeybindingStore";
export type { KeybindingMutationResult, KeybindingSnapshot } from "./KeybindingStore";
export {
  analyzeKeybindingConflicts,
  isUserKeybindingRuleUsable,
  keybindingModesMayOverlap,
  logicalKeyForCode,
  resolveEffectiveKeymap,
} from "./effectiveKeymap";
export type {
  EffectiveKeymap,
  KeybindingConflict,
  KeybindingConflictRule,
} from "./effectiveKeymap";
export {
  KEYBINDINGS_STORAGE_KEY,
  MAX_USER_KEYBINDINGS,
  normalizeUserKeybindingRule,
  parseUserKeybindingPreferences,
  readUserKeybindings,
} from "./keybindingPreferences";
export type { KnownUserKeybindingRule, UserKeybindingRule } from "./keybindingPreferences";
export {
  DESKTOP_ACTION_BINDING_VERSION,
  DESKTOP_MENU_ACTIONS,
  desktopActionBindingSnapshot,
  isDesktopMenuAction,
  keyStrokeToElectronAccelerator,
} from "./desktopActionBridge";
export type {
  DesktopActionBinding,
  DesktopActionBindingSnapshot,
  DesktopActionInvocation,
  DesktopMenuAction,
} from "./desktopActionBridge";
export { useRegisterAction } from "./useRegisterAction";
export type { ActionInvalidationKey } from "./useRegisterAction";
export { and, contextsMayOverlap, equals, not, or, when } from "./context";
export { formatKeybinding, formatKeybindingForAria, formatKeyStroke } from "./keybindingFormatter";
export { isMacKeyboardPlatform, keybindingEnvironmentExpression } from "./keybindingEnvironment";
export { parseKeybinding, serializeKeybinding } from "./keybindingParser";
export { isReservedEscapeSequence } from "./keybindingPolicy";
export type {
  ActionHandlerContext,
  ActionHandlerRegistration,
  ActionRegistrationFor,
  ActionResolution,
  AvailableAction,
  AvailablePaletteAction,
} from "./actionRegistry";
export type {
  ActionArgs,
  ActionArgsById,
  ActionDefinition,
  ActionId,
  ActionInvocation,
  ActionResult,
  ActionScopeId,
  ActionIconName,
  ArglessActionId,
  ActionSource,
  ContextPatch,
  ContextSnapshot,
  JsonValue,
  KeybindingMode,
  KeybindingRule,
  KeyModifier,
  KeyStroke,
} from "./types";
export { HANDLED, KEYBINDING_MODES, NOT_HANDLED } from "./types";
