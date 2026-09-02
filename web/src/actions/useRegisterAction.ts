import { useEffect, useLayoutEffect, useRef } from "react";
import type { ActionId } from "./types";
import type { ActionRegistrationFor } from "./actionRegistry";
import { useCurrentActionScopeId, useInternalActionRuntime } from "./ActionProvider";

export type ActionInvalidationKey = string | number | boolean | null | undefined;

/**
 * Register a live action implementation in the nearest action scope.
 * Change the primitive `invalidationKey` when enabled/visible predicates change.
 */
export function useRegisterAction<A extends ActionId>(
  action: A,
  registration: ActionRegistrationFor<A>,
  invalidationKey?: ActionInvalidationKey,
): void {
  const actions = useInternalActionRuntime();
  const currentScopeId = useCurrentActionScopeId();
  const scopeId = registration.scope === "global" ? null : currentScopeId;
  const latest = useRef(registration);
  latest.current = registration;

  useLayoutEffect(() => actions.registry.invalidate(), [actions.registry, invalidationKey]);

  useEffect(
    () =>
      actions.registry.registerAction({
        action,
        scopeId,
        run: (invocation, context) => latest.current.run(invocation, context),
        isEnabled: (context) => latest.current.isEnabled?.(context) !== false,
        isVisible: (context) => latest.current.isVisible?.(context) !== false,
        acceptsKeybindings: registration.acceptsKeybindings,
      }),
    [action, actions.registry, registration.acceptsKeybindings, scopeId],
  );
}
