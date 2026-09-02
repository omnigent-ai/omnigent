import {
  cloneElement,
  createContext,
  isValidElement,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type FocusEvent as ReactFocusEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useIsEmbedded } from "@/lib/embedded";
import { isElectronShell, isNativeShell } from "@/lib/nativeBridge";
import { ActionRegistry, type ActionResolution, type AvailableAction } from "./actionRegistry";
import { EMPTY_ACTION_CONTEXT } from "./context";
import type {
  ActionId,
  ActionInvocation,
  ActionResult,
  ActionScopeId,
  ActionSource,
  ArglessActionId,
  ContextPatch,
  ContextSnapshot,
  KeybindingMode,
} from "./types";
import { NOT_HANDLED } from "./types";

const ACTION_SCOPE_ATTRIBUTE = "data-action-scope";

function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const withData = navigator as Navigator & { userAgentData?: { platform?: string } };
  const platform = withData.userAgentData?.platform ?? navigator.platform ?? navigator.userAgent;
  return /Mac|iPhone|iPad|iPod/i.test(platform);
}

function eventElement(event?: KeyboardEvent): Element | null {
  if (event) {
    for (const entry of event.composedPath()) if (entry instanceof Element) return entry;
  }
  return document.activeElement instanceof Element ? document.activeElement : null;
}

export function actionScopeIdsFromElement(element: Element | null): ActionScopeId[] {
  const ids: ActionScopeId[] = [];
  let current: Element | null = element;
  while (current) {
    const id = current.getAttribute(ACTION_SCOPE_ATTRIBUTE);
    if (id) ids.push(id as ActionScopeId);
    current = current.parentElement;
  }
  return ids;
}

function isInputLike(element: Element | null): boolean {
  return Boolean(
    element?.closest('textarea, input, select, [contenteditable="true"], [contenteditable=""]'),
  );
}

function actionContext(
  event: KeyboardEvent | undefined,
  environment: ContextSnapshot,
): ContextSnapshot {
  const element = eventElement(event);
  return {
    ...environment,
    inputFocus: isInputLike(element),
    terminalFocus: Boolean(element?.closest(".xterm")),
    monacoFocus: Boolean(element?.closest(".monaco-editor")),
    markdownEditorFocus: Boolean(element?.closest(".ProseMirror")),
    eventMeta: event?.metaKey ?? false,
  };
}

export interface ActionsApi {
  execute: (invocation: ActionInvocation) => ActionResult;
  executeAction: (action: ArglessActionId, source: ActionSource) => ActionResult;
}

interface ActionsRuntime extends ActionsApi {
  registry: ActionRegistry;
  getResolution: (event?: KeyboardEvent) => ActionResolution;
}

const ActionsContext = createContext<ActionsRuntime | null>(null);
const ScopeContext = createContext<ActionScopeId | null>(null);

export function ActionsProvider({ children }: { children: ReactNode }) {
  const embedded = useIsEmbedded();
  const isCoarsePointer = useIsCoarsePointer();
  const registryRef = useRef<ActionRegistry | null>(null);
  if (!registryRef.current) registryRef.current = new ActionRegistry();
  const registry = registryRef.current;
  const environment = useMemo<ContextSnapshot>(
    () => ({
      ...EMPTY_ACTION_CONTEXT,
      isMac: isMacPlatform(),
      isNativeShell: isNativeShell(),
      isElectron: isElectronShell(),
      isEmbedded: embedded,
      isCoarsePointer,
    }),
    [embedded, isCoarsePointer],
  );

  const value = useMemo<ActionsRuntime>(() => {
    const getResolution = (event?: KeyboardEvent): ActionResolution => {
      const domScopeIds = actionScopeIdsFromElement(eventElement(event));
      return {
        context: actionContext(event, environment),
        focusedScopeIds: registry.expandFocusedScopeIds(domScopeIds),
      };
    };
    return {
      registry,
      getResolution,
      execute: (invocation) => registry.execute(invocation, getResolution(invocation.event)),
      executeAction: (action, source) =>
        registry.execute({ action, source } as ActionInvocation, getResolution()),
    };
  }, [environment, registry]);

  useEffect(() => {
    const invalidate = () => registry.invalidate();
    window.addEventListener("focusin", invalidate);
    window.addEventListener("focusout", invalidate);
    document.addEventListener("visibilitychange", invalidate);
    return () => {
      window.removeEventListener("focusin", invalidate);
      window.removeEventListener("focusout", invalidate);
      document.removeEventListener("visibilitychange", invalidate);
    };
  }, [registry]);

  return <ActionsContext.Provider value={value}>{children}</ActionsContext.Provider>;
}

const INERT_REGISTRY = new ActionRegistry(true);
const INERT_ACTIONS: ActionsRuntime = {
  registry: INERT_REGISTRY,
  getResolution: () => ({ context: EMPTY_ACTION_CONTEXT, focusedScopeIds: [] }),
  execute: () => NOT_HANDLED,
  executeAction: () => NOT_HANDLED,
};

function useActionRuntime(): ActionsRuntime {
  return useContext(ActionsContext) ?? INERT_ACTIONS;
}

export function useActions(): ActionsApi {
  const { execute, executeAction } = useActionRuntime();
  return useMemo(() => ({ execute, executeAction }), [execute, executeAction]);
}

export function useAvailableActions(
  options: {
    paletteOnly?: boolean;
  } = {},
): readonly AvailableAction[] {
  const actions = useActionRuntime();
  useSyncExternalStore(
    actions.registry.subscribe,
    actions.registry.getRevision,
    actions.registry.getRevision,
  );
  return actions.registry.listAvailable(actions.getResolution(), options);
}

const NO_SUBSCRIPTION = () => () => {};

/** Keep palette availability and execution anchored to the focus that opened it. */
export function usePaletteActions(enabled = true) {
  const runtime = useActionRuntime();
  useSyncExternalStore(
    enabled ? runtime.registry.subscribe : NO_SUBSCRIPTION,
    runtime.registry.getRevision,
    runtime.registry.getRevision,
  );
  const wasEnabled = useRef(false);
  const resolution = useRef<ActionResolution | null>(null);
  if (!resolution.current || !enabled || !wasEnabled.current) {
    resolution.current = runtime.getResolution();
  }
  wasEnabled.current = enabled;
  const captured = resolution.current;
  return {
    actions: runtime.registry.listPaletteActions(captured),
    executeAction: (action: ArglessActionId) =>
      // catalog.ts proves every palette action is argless.
      runtime.registry.execute({ action, source: "palette" } as ActionInvocation, captured),
  };
}

export interface ActionScopeOptions {
  mode: KeybindingMode;
  active?: boolean;
  context?: ContextPatch;
}

export interface ActionScopeHandle {
  id: ActionScopeId;
  rootProps: ScopeElementProps;
}

export interface ActionScopeProps extends ActionScopeOptions {
  children: ReactElement;
}

const EMPTY_CONTEXT_PATCH: ContextPatch = {};

interface ScopeElementProps {
  "data-action-scope"?: string;
  onFocusCapture?: (event: ReactFocusEvent) => void;
  onBlurCapture?: (event: ReactFocusEvent) => void;
}

/** Register a scope while letting an existing large root spread the marker props. */
export function useActionScopeRegistration({
  mode,
  active = true,
  context = EMPTY_CONTEXT_PATCH,
}: ActionScopeOptions): ActionScopeHandle {
  const actions = useActionRuntime();
  const parentId = useContext(ScopeContext);
  const reactId = useId();
  const id = `action-scope-${reactId}` as ActionScopeId;
  const latest = useRef({ mode, active, context });
  const focusGeneration = useRef(0);
  latest.current = { mode, active, context };

  useLayoutEffect(() => {
    const current = latest.current;
    return actions.registry.registerScope({ id, parentId, ...current });
  }, [actions.registry, id, parentId]);
  useLayoutEffect(() => {
    actions.registry.updateScope(id, { mode, active, context, parentId });
  }, [actions.registry, active, context, id, mode, parentId]);

  return {
    id,
    rootProps: {
      "data-action-scope": id,
      onFocusCapture: (event) => {
        if (event.target instanceof Element) {
          focusGeneration.current = actions.registry.markFocusedScope(id, event.target);
        }
      },
      onBlurCapture: () => {
        const generation = focusGeneration.current;
        queueMicrotask(() => actions.registry.clearFocusedScope(generation));
      },
    },
  };
}

export function ActionScopeProvider({
  scope,
  children,
}: {
  scope: ActionScopeHandle;
  children: ReactNode;
}) {
  useLayoutEffect(() => {
    if (!import.meta.env.DEV) return;
    const markerExists = document.querySelector(`[${ACTION_SCOPE_ATTRIBUTE}="${scope.id}"]`);
    if (!markerExists) console.error(`Action scope ${scope.id} did not spread rootProps`);
  }, [scope.id]);
  return <ScopeContext.Provider value={scope.id}>{children}</ScopeContext.Provider>;
}

/**
 * Stamp action scope state onto one intrinsic DOM child without changing layout.
 * Wrap custom components or fragments in their existing root DOM element.
 */
export function ActionScope({ children, ...options }: ActionScopeProps) {
  const scope = useActionScopeRegistration(options);
  const child = children;
  if (!isValidElement<ScopeElementProps>(child) || typeof child.type !== "string") {
    throw new Error("ActionScope requires one intrinsic DOM element child");
  }
  const originalFocus = child.props.onFocusCapture;
  const originalBlur = child.props.onBlurCapture;
  const scopedChild = cloneElement(child, {
    ...scope.rootProps,
    onFocusCapture: (event: ReactFocusEvent) => {
      originalFocus?.(event);
      scope.rootProps.onFocusCapture?.(event);
    },
    onBlurCapture: (event: ReactFocusEvent) => {
      originalBlur?.(event);
      scope.rootProps.onBlurCapture?.(event);
    },
  });

  return <ActionScopeProvider scope={scope}>{scopedChild}</ActionScopeProvider>;
}

export function useCurrentActionScopeId(): ActionScopeId | null {
  return useContext(ScopeContext);
}

export function useActionAvailable(action: ActionId): boolean {
  const actions = useActionRuntime();
  useSyncExternalStore(
    actions.registry.subscribe,
    actions.registry.getRevision,
    actions.registry.getRevision,
  );
  return actions.registry.canHandle(action, actions.getResolution());
}

export { useActionRuntime as useInternalActionRuntime };
