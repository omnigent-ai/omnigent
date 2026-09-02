import type {
  ActionContextValues,
  BooleanContextKey,
  ContextExpression,
  ContextKey,
  ContextSnapshot,
  ContextValue,
} from "./types";

export const CONTEXT_KEYS = {
  isMac: "isMac",
  isNativeShell: "isNativeShell",
  isElectron: "isElectron",
  isEmbedded: "isEmbedded",
  isCoarsePointer: "isCoarsePointer",
  inputFocus: "inputFocus",
  terminalFocus: "terminalFocus",
  monacoFocus: "monacoFocus",
  markdownEditorFocus: "markdownEditorFocus",
  eventMeta: "eventMeta",
  composerStreaming: "composerStreaming",
  composerSuggestionsOpen: "composerSuggestionsOpen",
  composerEnterInserts: "composerEnterInserts",
  composerSubmitWithModEnter: "composerSubmitWithModEnter",
  fileSearchOpen: "fileSearchOpen",
} as const satisfies Record<ContextKey, ContextKey>;

/** Complete baseline; active scopes and the current event overlay patches. */
export const EMPTY_ACTION_CONTEXT: ContextSnapshot = {
  isMac: false,
  isNativeShell: false,
  isElectron: false,
  isEmbedded: false,
  isCoarsePointer: false,
  inputFocus: false,
  terminalFocus: false,
  monacoFocus: false,
  markdownEditorFocus: false,
  eventMeta: false,
  composerStreaming: false,
  composerSuggestionsOpen: false,
  composerEnterInserts: false,
  composerSubmitWithModEnter: false,
  fileSearchOpen: false,
};

export function when(key: BooleanContextKey): ContextExpression {
  return { type: "truthy", key };
}

export function equals<K extends ContextKey>(
  key: K,
  value: ActionContextValues[K] | null,
): ContextExpression {
  return { type: "equals", key, value } as ContextExpression;
}

export function not(expression: ContextExpression): ContextExpression {
  return { type: "not", expression };
}

export function and(...expressions: readonly ContextExpression[]): ContextExpression {
  const flattened = expressions.flatMap((expression) =>
    expression.type === "and" ? expression.expressions : [expression],
  );
  return { type: "and", expressions: flattened };
}

export function or(...expressions: readonly ContextExpression[]): ContextExpression {
  const flattened = expressions.flatMap((expression) =>
    expression.type === "or" ? expression.expressions : [expression],
  );
  return { type: "or", expressions: flattened };
}

export function evaluateContext(
  expression: ContextExpression | undefined,
  context: ContextSnapshot,
): boolean {
  if (!expression) return true;
  switch (expression.type) {
    case "truthy":
      return Boolean(context[expression.key]);
    case "equals":
      return context[expression.key] === expression.value;
    case "not":
      return !evaluateContext(expression.expression, context);
    case "and":
      return expression.expressions.every((child) => evaluateContext(child, context));
    case "or":
      return expression.expressions.some((child) => evaluateContext(child, context));
  }
}

/** Atomic constraints along the narrowest matching branch. */
export function contextSpecificity(expression: ContextExpression | undefined): number {
  if (!expression) return 0;
  switch (expression.type) {
    case "truthy":
    case "equals":
      return 1;
    case "not":
      return contextSpecificity(expression.expression);
    case "and":
      return expression.expressions.reduce((total, child) => total + contextSpecificity(child), 0);
    case "or":
      return expression.expressions.length === 0
        ? 0
        : Math.min(...expression.expressions.map(contextSpecificity));
  }
}

type ComparableValue = Exclude<ContextValue, undefined>;

interface Constraint {
  equals?: ComparableValue;
  excludes: Set<ComparableValue>;
}

type Clause = Map<string, Constraint>;

/** A malformed/future expression cannot make conflict analysis explode. */
const MAX_DNF_CLAUSES = 256;

function cloneClause(clause: Clause): Clause {
  return new Map(
    [...clause].map(([key, constraint]) => [
      key,
      { equals: constraint.equals, excludes: new Set(constraint.excludes) },
    ]),
  );
}

function mergeConstraint(
  clause: Clause,
  key: string,
  value: ComparableValue,
  negated: boolean,
): Clause | null {
  const next = cloneClause(clause);
  const constraint = next.get(key) ?? { excludes: new Set<ComparableValue>() };
  if (negated) {
    if (constraint.equals === value) return null;
    constraint.excludes.add(value);
  } else {
    if (constraint.equals !== undefined && constraint.equals !== value) return null;
    if (constraint.excludes.has(value)) return null;
    constraint.equals = value;
  }
  next.set(key, constraint);
  return next;
}

/** Null means the conservative clause cap was exceeded. */
function clausesFor(expression: ContextExpression, negated = false): Clause[] | null {
  switch (expression.type) {
    case "truthy": {
      const clause = mergeConstraint(new Map(), expression.key, true, negated);
      return clause ? [clause] : [];
    }
    case "equals": {
      const clause = mergeConstraint(new Map(), expression.key, expression.value, negated);
      return clause ? [clause] : [];
    }
    case "not":
      return clausesFor(expression.expression, !negated);
    case "and":
    case "or": {
      const conjunction = (expression.type === "and") !== negated;
      if (!conjunction) {
        const out: Clause[] = [];
        for (const child of expression.expressions) {
          const childClauses = clausesFor(child, negated);
          if (!childClauses) return null;
          out.push(...childClauses);
          if (out.length > MAX_DNF_CLAUSES) return null;
        }
        return out;
      }
      let clauses: Clause[] = [new Map()];
      for (const child of expression.expressions) {
        const childClauses = clausesFor(child, negated);
        if (!childClauses) return null;
        const combined: Clause[] = [];
        for (const left of clauses) {
          for (const right of childClauses) {
            let merged: Clause | null = cloneClause(left);
            for (const [key, constraint] of right) {
              if (constraint.equals !== undefined) {
                merged = mergeConstraint(merged, key, constraint.equals, false);
                if (!merged) break;
              }
              for (const excluded of constraint.excludes) {
                merged = mergeConstraint(merged, key, excluded, true);
                if (!merged) break;
              }
              if (!merged) break;
            }
            if (merged) combined.push(merged);
            if (combined.length > MAX_DNF_CLAUSES) return null;
          }
        }
        clauses = combined;
      }
      return clauses;
    }
  }
}

function clausesCompatible(left: Clause, right: Clause): boolean {
  let merged: Clause | null = cloneClause(left);
  for (const [key, constraint] of right) {
    if (constraint.equals !== undefined) {
      merged = mergeConstraint(merged, key, constraint.equals, false);
      if (!merged) return false;
    }
    for (const excluded of constraint.excludes) {
      merged = mergeConstraint(merged, key, excluded, true);
      if (!merged) return false;
    }
  }
  return true;
}

/**
 * Whether two predicates can both be true. If an expression exceeds the
 * bounded DNF analysis, report a possible overlap rather than miss a conflict.
 */
export function contextsMayOverlap(
  left: ContextExpression | undefined,
  right: ContextExpression | undefined,
): boolean {
  if (!left || !right) return true;
  const leftClauses = clausesFor(left);
  const rightClauses = clausesFor(right);
  if (!leftClauses || !rightClauses) return true;
  return leftClauses.some((a) => rightClauses.some((b) => clausesCompatible(a, b)));
}
