import { ACTION_CATALOG } from "./catalog";
import type {
  ActionArgs,
  ActionDefinition,
  ActionId,
  ActionInvocation,
  ActionResult,
  ContextPatch,
  ContextSnapshot,
  KeybindingMode,
  KeybindingRule,
} from "./types";
import { HANDLED, NOT_HANDLED } from "./types";

export interface ActionScopeRecord {
  id: string;
  parentId: string | null;
  mode: KeybindingMode;
  active: boolean;
  context: ContextPatch;
}

export interface ActionHandlerContext {
  context: ContextSnapshot;
  scopeId: string | null;
}

export interface ActionHandlerRegistration<A extends ActionId = ActionId> {
  action: A;
  scopeId: string | null;
  run: (
    invocation: Extract<ActionInvocation, { action: A }>,
    context: ActionHandlerContext,
  ) => ActionResult | void | Promise<ActionResult | void>;
  isEnabled?: (context: ActionHandlerContext) => boolean;
  isVisible?: (context: ActionHandlerContext) => boolean;
}

interface RegisteredHandler {
  action: ActionId;
  scopeId: string | null;
  run: (
    invocation: ActionInvocation,
    context: ActionHandlerContext,
  ) => ActionResult | void | Promise<ActionResult | void>;
  isEnabled?: (context: ActionHandlerContext) => boolean;
  isVisible?: (context: ActionHandlerContext) => boolean;
  token: number;
  order: number;
}

export interface ActionResolution {
  context: ContextSnapshot;
  /** Inner-most scope first. */
  focusedScopeIds: readonly string[];
}

export interface AvailableAction extends ActionDefinition {
  enabled: boolean;
}

interface RankedHandler {
  handler: RegisteredHandler;
  context: ActionHandlerContext;
  rank: number;
}

export class ActionRegistry {
  private readonly scopes = new Map<string, ActionScopeRecord>();
  private readonly handlers = new Map<ActionId, Map<number, RegisteredHandler>>();
  private readonly listeners = new Set<() => void>();
  private nextToken = 1;
  private nextOrder = 1;
  private revision = 0;
  private focusedScopeId: string | null = null;
  private focusedTarget: Element | null = null;
  private focusGeneration = 0;
  private readonly inert: boolean;

  constructor(inert = false) {
    this.inert = inert;
  }

  subscribe = (listener: () => void): (() => void) => {
    if (this.inert) return () => {};
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getRevision = (): number => this.revision;

  invalidate = (): void => {
    if (!this.inert) this.changed();
  };

  private changed(): void {
    this.revision += 1;
    for (const listener of this.listeners) listener();
  }

  registerScope(scope: ActionScopeRecord): () => void {
    if (this.inert) return () => {};
    this.scopes.set(scope.id, scope);
    this.changed();
    return () => {
      if (this.scopes.delete(scope.id)) this.changed();
    };
  }

  updateScope(id: string, patch: Partial<Omit<ActionScopeRecord, "id">>): void {
    if (this.inert) return;
    const current = this.scopes.get(id);
    if (!current) return;
    const next = { ...current, ...patch };
    const contextUnchanged =
      Object.keys(current.context).length === Object.keys(next.context).length &&
      Object.entries(current.context).every(
        ([key, value]) => next.context[key as keyof ContextPatch] === value,
      );
    if (
      current.parentId === next.parentId &&
      current.mode === next.mode &&
      current.active === next.active &&
      contextUnchanged
    ) {
      return;
    }
    this.scopes.set(id, next);
    this.changed();
  }

  registerAction<A extends ActionId>(registration: ActionHandlerRegistration<A>): () => void {
    if (this.inert) return () => {};
    const token = this.nextToken++;
    const handler: RegisteredHandler = {
      action: registration.action,
      scopeId: registration.scopeId,
      run: (invocation, context) =>
        registration.run(invocation as Extract<ActionInvocation, { action: A }>, context),
      isEnabled: registration.isEnabled,
      isVisible: registration.isVisible,
      token,
      order: this.nextOrder++,
    };
    const byAction = this.handlers.get(registration.action) ?? new Map();
    byAction.set(token, handler);
    this.handlers.set(registration.action, byAction);
    this.changed();
    return () => {
      const entries = this.handlers.get(registration.action);
      if (!entries?.delete(token)) return;
      if (entries.size === 0) this.handlers.delete(registration.action);
      this.changed();
    };
  }

  getScope(id: string): ActionScopeRecord | undefined {
    return this.scopes.get(id);
  }

  markFocusedScope(id: string, target: Element): number {
    if (this.inert) return 0;
    this.focusedScopeId = id;
    this.focusedTarget = target;
    this.focusGeneration += 1;
    this.changed();
    return this.focusGeneration;
  }

  clearFocusedScope(generation?: number): void {
    if (this.inert) return;
    if (generation !== undefined && generation !== this.focusGeneration) return;
    if (this.focusedScopeId === null) return;
    this.focusedScopeId = null;
    this.focusedTarget = null;
    this.focusGeneration += 1;
    this.changed();
  }

  /** Expand DOM markers through React scope parents, including across portals. */
  expandFocusedScopeIds(domScopeIds: readonly string[]): string[] {
    const activeElement = typeof document === "undefined" ? null : document.activeElement;
    const rememberedFocusIsCurrent = Boolean(
      this.focusedScopeId &&
      this.focusedTarget?.isConnected &&
      activeElement instanceof Element &&
      (this.focusedTarget === activeElement || this.focusedTarget.contains(activeElement)),
    );
    const seeds =
      domScopeIds.length > 0
        ? domScopeIds
        : rememberedFocusIsCurrent && this.focusedScopeId
          ? [this.focusedScopeId]
          : [];
    const expanded: string[] = [];
    const seen = new Set<string>();
    for (const seed of seeds) {
      let current = this.scopes.get(seed);
      while (current && !seen.has(current.id)) {
        seen.add(current.id);
        expanded.push(current.id);
        current = current.parentId ? this.scopes.get(current.parentId) : undefined;
      }
    }
    return expanded;
  }

  getActiveModes(): ReadonlySet<KeybindingMode> {
    const modes = new Set<KeybindingMode>(["global"]);
    for (const scope of this.scopes.values()) if (scope.active) modes.add(scope.mode);
    return modes;
  }

  getFocusedModes(focusedScopeIds: readonly string[]): ReadonlySet<KeybindingMode> {
    const modes = new Set<KeybindingMode>(["global"]);
    for (const id of focusedScopeIds) {
      const scope = this.scopes.get(id);
      if (scope?.active) modes.add(scope.mode);
    }
    return modes;
  }

  contextForResolution(resolution: ActionResolution): ContextSnapshot {
    return this.contextForScope(resolution.focusedScopeIds[0] ?? null, resolution.context);
  }

  contextsForRule(rule: KeybindingRule, resolution: ActionResolution): readonly ContextSnapshot[] {
    if ((rule.activation ?? "focused") !== "active" || rule.mode === "global") {
      return [this.contextForResolution(resolution)];
    }
    const contexts = [...this.scopes.values()]
      .filter((scope) => scope.active && scope.mode === rule.mode)
      .map((scope) => this.contextForScope(scope.id, resolution.context));
    return contexts.length > 0 ? contexts : [resolution.context];
  }

  private contextForScope(scopeId: string | null, base: ContextSnapshot): ContextSnapshot {
    if (!scopeId) return base;
    const lineage: ActionScopeRecord[] = [];
    const seen = new Set<string>();
    let current = this.scopes.get(scopeId);
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      lineage.push(current);
      current = current.parentId ? this.scopes.get(current.parentId) : undefined;
    }
    const merged = { ...base };
    for (const scope of lineage.reverse()) Object.assign(merged, scope.context);
    return merged;
  }

  private rankedHandlers(action: ActionId, resolution: ActionResolution): RankedHandler[] {
    const entries = this.handlers.get(action);
    if (!entries) return [];
    const focusedRanks = new Map(
      resolution.focusedScopeIds.map((scopeId, index) => [scopeId, 30_000 - index]),
    );
    const ranked: RankedHandler[] = [];
    for (const handler of entries.values()) {
      let rank: number;
      if (handler.scopeId === null) {
        rank = 10_000;
      } else {
        const focusedRank = focusedRanks.get(handler.scopeId);
        if (focusedRank !== undefined) rank = focusedRank;
        else if (this.scopes.get(handler.scopeId)?.active) rank = 20_000;
        else continue;
      }
      const context = {
        context: this.contextForScope(handler.scopeId, resolution.context),
        scopeId: handler.scopeId,
      };
      ranked.push({ handler, context, rank });
    }
    return ranked.sort(
      (left, right) => right.rank - left.rank || right.handler.order - left.handler.order,
    );
  }

  canHandle(action: ActionId, resolution: ActionResolution): boolean {
    if (this.inert) return false;
    return this.rankedHandlers(action, resolution).some(
      ({ handler, context }) => handler.isEnabled?.(context) !== false,
    );
  }

  execute(invocation: ActionInvocation, resolution: ActionResolution): ActionResult {
    if (this.inert) return NOT_HANDLED;
    for (const { handler, context } of this.rankedHandlers(invocation.action, resolution)) {
      if (handler.isEnabled?.(context) === false) continue;
      const result = handler.run(invocation, context);
      if (result === NOT_HANDLED) continue;
      // Void and promises count as handled. Once async work starts there is no
      // safe synchronous fallback, but rejection must not escape unobserved.
      if (typeof result === "object" && result !== null) {
        void Promise.resolve(result).catch((error: unknown) => {
          console.error(`Action ${invocation.action} failed`, error);
        });
      }
      return HANDLED;
    }
    return NOT_HANDLED;
  }

  listAvailable(
    resolution: ActionResolution,
    options: { paletteOnly?: boolean } = {},
  ): readonly AvailableAction[] {
    if (this.inert) return [];
    return ACTION_CATALOG.flatMap((definition) => {
      if (options.paletteOnly && definition.palette !== true) return [];
      const candidates = this.rankedHandlers(definition.id, resolution).filter(
        ({ handler, context }) => handler.isVisible?.(context) !== false,
      );
      if (candidates.length === 0) return [];
      return [
        {
          ...definition,
          enabled: candidates.some(
            ({ handler, context }) => handler.isEnabled?.(context) !== false,
          ),
        },
      ];
    });
  }
}

export type ActionRegistrationFor<A extends ActionId> = Omit<
  ActionHandlerRegistration<A>,
  "action" | "scopeId"
>;

export type ActionArgsFor<A extends ActionId> = ActionArgs<A>;
