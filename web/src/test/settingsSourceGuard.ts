// The source guard for searchable Settings. It reads the Settings code with the TypeScript
// compiler, not the rendered page, so a control in a branch no test renders is checked too.
// Every element that creates a control must be owned by a catalog entry, through one of:
//
//   - spreading `settingsControlProps(entry, part?)` (or, for overlay content, `settingsOverlayProps`);
//   - being an option of such a control, or of an owned overlay;
//   - a declared row action or generated field inside a <SettingsDataRegion> row template, or
//     inside the dialog one of its row actions opens (`settingsRowOverlayProps`);
//   - a declared dialog action button inside an owned overlay.
//
// The walk starts at the Settings modules and follows every component, helper, render prop and
// lazy import they reach, outside the ui/ primitives and node_modules.

import path from "node:path";

import ts from "typescript";

import { isSettingsEntryFamily, type SettingsEntry } from "@/pages/settings/search/catalog/types";

export interface GuardViolation {
  /** `file › function › element`, for people. */
  site: string;
  file: string;
  line: number;
  reason: string;
}

export interface SettingsGuardInput {
  program: ts.Program;
  webRoot: string;
  /** Modules scanned whole: every function in them, rendered or not. */
  rootFiles: readonly string[];
  /**
   * Instead of scanning `rootFiles` whole, walk only from these top-level functions, as their
   * page renders them: one section's controls, with nothing else in its module.
   */
  entryFunctions?: readonly { file: string; name: string }[];
  /** Catalog entries by `absolute file#export`. */
  catalog: ReadonlyMap<string, SettingsEntry>;
  /** Entry families (`settingsEntryFamily`) by `absolute file#export`: each member's entry. */
  families?: ReadonlyMap<string, ReadonlyMap<string, SettingsEntry>>;
}

const UI_DIR = `${path.sep}src${path.sep}components${path.sep}ui${path.sep}`;
const SEARCH_DIR = `${path.sep}src${path.sep}pages${path.sep}settings${path.sep}search${path.sep}`;
const CATALOG_DIR = `${SEARCH_DIR}catalog${path.sep}`;
const NOT_TRAVERSED = [
  `${path.sep}src${path.sep}components${path.sep}SettingsLabel.tsx`,
  `${path.sep}src${path.sep}lib${path.sep}routing`,
];

/** ui/ primitives that are one control each. */
const UI_CONTROLS = new Set([
  "Button",
  "Switch",
  "Input",
  "Textarea",
  "Checkbox",
  "SelectTrigger",
  "TabsTrigger",
  "InputGroupButton",
  "InputGroupInput",
  "InputGroupTextarea",
  "CommandInput",
  "MenuItem",
  "DropdownMenuItem",
  "DropdownMenuSubTrigger",
  "ContextMenuItem",
  "ContextMenuSubTrigger",
]);
/** ui/ triggers that render their own button unless `asChild` hands that to a child. */
const UI_TRIGGERS = new Set([
  "DialogTrigger",
  "PopoverTrigger",
  "DropdownMenuTrigger",
  "ContextMenuTrigger",
  "CollapsibleTrigger",
  "AccordionTrigger",
  "HoverCardTrigger",
  "DialogClose",
]);
/** ui/ primitives that are one value of the control around them. */
const UI_OPTIONS = new Set([
  "SelectItem",
  "DropdownMenuRadioItem",
  "DropdownMenuCheckboxItem",
  "ContextMenuRadioItem",
  "ContextMenuCheckboxItem",
  "CommandItem",
]);
/** ui/ overlay content, which renders in a portal outside the row; a `<datalist>` is one too. */
const UI_OVERLAYS = new Set([
  "DialogContent",
  "PopoverContent",
  "DropdownMenuContent",
  "DropdownMenuSubContent",
  "ContextMenuContent",
  "ContextMenuSubContent",
  "SelectContent",
  "HoverCardContent",
  "CommandDialog",
]);
// A <summary> opens its <details>, so every authored one is a control.
const INTRINSIC_CONTROLS = new Set(["input", "select", "textarea", "button", "summary"]);
const INTERACTIVE_ROLES = new Set([
  "button",
  "link",
  "switch",
  "checkbox",
  "radio",
  "combobox",
  "slider",
  "spinbutton",
  "tab",
  "textbox",
  "menuitem",
  "menuitemradio",
  "menuitemcheckbox",
  "option",
]);
const OPTION_ROLES = new Set(["radio", "option", "menuitemradio", "menuitemcheckbox"]);
const ROW_ACTION_CONTROLS = new Set([
  "button",
  "a",
  "Button",
  "Checkbox",
  "Switch",
  "Link",
  "input",
  "summary",
]);
/** Controls that take a typed value: the only ones a row field may mark. */
const ROW_FIELD_CONTROLS = new Set([
  "input",
  "textarea",
  "select",
  "Input",
  "Textarea",
  "SelectTrigger",
]);
/** Input types that act rather than take a value. */
const NON_FIELD_INPUT_TYPES = new Set(["button", "submit", "reset", "image", "checkbox", "radio"]);

/** What a family lookup reads, and the entry id each member it may read maps to. */
interface FamilyLookupValue {
  key: string;
  entryByMember: ReadonlyMap<string, string>;
}

interface Owner {
  /** The entry, or for a family lookup its first member; `family` then lists every member. */
  entry?: SettingsEntry;
  /** A family lookup's members: each must satisfy every check, not just the first. */
  family?: readonly SettingsEntry[];
  /**
   * For a family lookup whose member isn't one literal, the value it reads and which entry each
   * possible member maps to: two lookups of one value through the same mapping find one entry.
   */
  lookup?: FamilyLookupValue;
  part?: string;
  /** Every part a control may be, when its part is a finite union of literals; `part` is the first. */
  candidates?: readonly string[];
}

interface Context {
  /** Inside an owned tab list: its tabs are that part's choices. */
  ownedTabList?: boolean;
  /** Inside a `settingsContentComponents` map: only plain links. */
  content?: boolean;
  /** Inside an owned control: only its options are allowed. */
  ownedControl?: Owner;
  /** Inside owned overlay content. */
  overlay?: Owner;
  region?: {
    /** The entry whose row the region sits in. */
    owner?: Owner;
    actions: ReadonlySet<string>;
    /** The actions named by their row's data, which sit only inside a marked region row. */
    dataNamed: ReadonlySet<string>;
    fields: ReadonlySet<string>;
    inRow: boolean;
    /** Inside an element marked `data-settings-region-row` in the row template. */
    inRegionRow?: boolean;
    /** The one `.map(...)` the region wraps: its row template. */
    template?: ts.Node;
  };
  /** Inside <SettingsOwner> in the JSX being walked. */
  settingsOwner?: Owner;
  /** Inside <SettingsItem entry>: the row a hidden file input may bind a part of. */
  item?: Owner;
  /** Functions being walked, innermost last, each with the SettingsOwner it was rendered under. */
  scopes?: readonly Scope[];
  /** Inside <SettingsOverlayOwner>, or a narrowed getter's Provider, in the JSX being walked. */
  overlayOwner?: OverlayOwnerContext;
}

/** A walked function and the owners its `useSettingsOwnership()` and `useSettingsOverlayParts()` read. */
interface Scope {
  node: ts.Node;
  owner?: Owner;
  overlayOwner?: OverlayOwnerContext;
}

/** The overlay a shared dialog is rendered in: its entry's candidates, as narrowed, and its opener. */
interface OverlayOwnerContext {
  owner: Owner;
  opener: string;
  /** The entries the owner may still be: every candidate, or those a `within` keeps. */
  entries: readonly SettingsEntry[];
}

/**
 * What a `useSettingsOverlayParts()` getter, or a `within` of one, holds where it's read: an
 * owner, none at all (rendered with no Settings owner), an owner proven outside its narrowing,
 * or one the guard can't tell, such as a narrowing whose members aren't a literal list.
 */
type OverlayGetter =
  | { kind: "owned"; owner: OverlayOwnerContext }
  | { kind: "unowned" }
  | { kind: "empty" }
  | { kind: "unknown"; reason: string };

type TagKind =
  | { kind: "intrinsic"; name: string }
  | { kind: "ui"; name: string }
  | { kind: "link" }
  | { kind: "search"; name: string; declaration: ts.Declaration }
  | { kind: "component"; key: string; declaration: ts.Declaration }
  | { kind: "external" };

export function createSettingsProgram(
  webRoot: string,
  rootFiles: readonly string[],
  virtualFiles: ReadonlyMap<string, string> = new Map(),
): ts.Program {
  const configPath = path.join(webRoot, "tsconfig.app.json");
  const parsed = ts.getParsedCommandLineOfConfigFile(
    configPath,
    {},
    {
      ...ts.sys,
      onUnRecoverableConfigFileDiagnostic: (diagnostic) => {
        throw new Error(ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"));
      },
    },
  );
  if (!parsed) throw new Error(`Cannot read ${configPath}`);
  const options = { ...parsed.options, noEmit: true };
  const host = ts.createCompilerHost(options);
  const readFile = host.readFile.bind(host);
  const fileExists = host.fileExists.bind(host);
  const getSourceFile = host.getSourceFile.bind(host);
  host.readFile = (fileName) => virtualFiles.get(fileName) ?? readFile(fileName);
  host.fileExists = (fileName) => virtualFiles.has(fileName) || fileExists(fileName);
  const directoryExists = host.directoryExists?.bind(host);
  host.directoryExists = (directory) =>
    [...virtualFiles.keys()].some((file) => file.startsWith(`${directory}${path.sep}`)) ||
    (directoryExists?.(directory) ?? ts.sys.directoryExists(directory));
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) => {
    const text = virtualFiles.get(fileName);
    return text === undefined
      ? getSourceFile(fileName, languageVersion, onError, shouldCreate)
      : ts.createSourceFile(fileName, text, languageVersion, true, ts.ScriptKind.TSX);
  };
  return ts.createProgram({ rootNames: [...rootFiles, ...virtualFiles.keys()], options, host });
}

/** `file#name` for a declaration, the key catalog entries are found by. */
export function declarationKey(declaration: ts.Declaration, name: string): string {
  return `${declaration.getSourceFile().fileName}#${name}`;
}

export function guardSettingsSource(input: SettingsGuardInput): GuardViolation[] {
  if (input.entryFunctions) return new SettingsSourceGuard(input).run();
  // Whole tree: an entry that is only rendered inside owners is checked through them, so a
  // survey walk finds those entries first and the real walk leaves them out.
  const ownerOnlyEntries = new SettingsSourceGuard(input).ownerOnlyEntries();
  return new SettingsSourceGuard(input, ownerOnlyEntries).run();
}

/** `file:position` for a function, the key a walk records it under. */
function nodeKey(node: ts.Node): string {
  return `${node.getSourceFile().fileName}:${node.pos}`;
}

/** How a walk reached a function: under an owner, and from which entry it was reached unowned. */
interface Reach {
  owned: boolean;
  /** Entries whose walks reached the function without an owner; module-level code is `""`. */
  unownedFrom: Set<string>;
}

class SettingsSourceGuard {
  private readonly checker: ts.TypeChecker;
  private readonly violations = new Map<string, GuardViolation>();
  private readonly visited = new Set<string>();
  private readonly reaches = new Map<string, Reach>();
  /** The entry whose walk is in progress; module-level code has none. */
  private currentEntry = "";
  /** Whole-tree entries walked on their own, as opposed to skipped. */
  private readonly walkedEntries = new Set<string>();
  private entryMembers: Map<SettingsEntry, string> | undefined;
  /** `settingsContentComponents(...)` calls, walked after everything else. */
  private readonly pendingContentMaps: {
    call: ts.CallExpression;
    context: Context;
    name: string;
  }[] = [];
  /** The maps a valid content site renders through its markdown. */
  private readonly trustedContentMaps = new Set<ts.CallExpression>();
  /** The markdown elements of valid content sites: the only places a content map may render. */
  private readonly trustedMarkdown = new Set<ts.Node>();
  private readonly constReadsCache = new Map<ts.SourceFile, Map<ts.Declaration, ts.Identifier[]>>();
  private importersCache: Map<ts.SourceFile, Set<ts.SourceFile>> | undefined;
  /** The one source element that renders each `entry:region`. */
  private readonly regionSites = new Map<string, ts.Node>();
  private readonly scannedRootFiles = new Set<string>();
  private readonly pendingRoots: string[] = [];

  private readonly input: SettingsGuardInput;

  /** Whole-tree entries left out, as walked through their owners instead. */
  private readonly skippedEntries: ReadonlySet<string>;

  constructor(input: SettingsGuardInput, skippedEntries: ReadonlySet<string> = new Set()) {
    this.input = input;
    this.skippedEntries = skippedEntries;
    this.checker = input.program.getTypeChecker();
  }

  /**
   * Entries a walk reaches under an owner, where every unowned walk reaching them starts at
   * another such entry or at itself. Each is checked through its owners, not on its own.
   */
  ownerOnlyEntries(): Set<string> {
    this.run();
    const ownerOnly = new Set(
      [...this.walkedEntries].filter((key) => this.reaches.get(key)?.owned),
    );
    let removed = true;
    while (removed) {
      removed = false;
      for (const key of ownerOnly) {
        const unownedFrom = this.reaches.get(key)!.unownedFrom;
        if ([...unownedFrom].some((entry) => entry !== key && !ownerOnly.has(entry))) {
          ownerOnly.delete(key);
          removed = true;
        }
      }
    }
    return ownerOnly;
  }

  run(): GuardViolation[] {
    if (this.input.entryFunctions) {
      for (const { file, name } of this.input.entryFunctions) this.visitEntryFunction(file, name);
    } else {
      this.pendingRoots.push(...this.input.rootFiles.map((file) => path.resolve(file)));
    }
    while (this.pendingRoots.length > 0) {
      const file = this.pendingRoots.pop()!;
      if (this.scannedRootFiles.has(file)) continue;
      this.scannedRootFiles.add(file);
      const source = this.input.program.getSourceFile(file);
      if (!source) throw new Error(`The Settings guard can't read ${file}`);
      this.scanRootModule(source);
    }
    this.checkContentMaps();
    return [...this.violations.values()].sort((first, second) =>
      first.site === second.site ? first.line - second.line : first.site < second.site ? -1 : 1,
    );
  }

  private visitEntryFunction(file: string, name: string) {
    const source = this.input.program.getSourceFile(path.resolve(file));
    if (!source) throw new Error(`The Settings guard can't read ${file}`);
    const found = source.statements
      .flatMap((statement) => topLevelFunctions(statement))
      .find((candidate) => candidate.name === name);
    if (!found) throw new Error(`${file} has no top-level function ${name}`);
    this.visitFunction(found.node, name, {});
  }

  // A root module's functions are checked whole. Ones it renders itself are checked where they
  // render, with that context (a data row, an owned control); the rest as entry points.
  private scanRootModule(source: ts.SourceFile) {
    const referenced = this.renderedLocally(source);
    for (const statement of source.statements) {
      const functions = topLevelFunctions(statement);
      if (functions.length === 0) {
        this.visit(statement, {});
        continue;
      }
      for (const { name, node } of functions) {
        if (referenced.has(name)) continue;
        const key = nodeKey(node);
        if (this.skippedEntries.has(key)) continue;
        this.walkedEntries.add(key);
        this.currentEntry = key;
        this.visitFunction(node, name, {});
        this.currentEntry = "";
      }
    }
  }

  /** Names of the module's own functions it uses anywhere: as a tag, a value or a callee. */
  private renderedLocally(source: ts.SourceFile): Set<string> {
    const names = new Set<string>();
    const walk = (node: ts.Node) => {
      if (isSkippedSubtree(node)) return;
      if (ts.isIdentifier(node) && !isDeclarationName(node)) {
        const declaration = this.resolveIdentifier(node)?.declaration;
        if (declaration?.getSourceFile() === source) names.add(node.text);
      }
      ts.forEachChild(node, walk);
    };
    walk(source);
    return names;
  }

  private visitFunction(node: ts.Node, name: string, context: Context) {
    this.recordReach(node, context);
    const key = `${node.getSourceFile().fileName}:${node.pos}|${contextSignature(context)}`;
    if (this.visited.has(key)) return;
    this.visited.add(key);
    // A hook here reads this function's owner or an enclosing one's, so only those are kept.
    const enclosing = (context.scopes ?? []).filter(
      (scope) => scope.node !== node && contains(scope.node, node),
    );
    const scopes = [
      ...enclosing,
      { node, owner: context.settingsOwner, overlayOwner: context.overlayOwner },
    ];
    this.visit(node, { ...context, scopes }, name);
  }

  private recordReach(node: ts.Node, context: Context) {
    const key = nodeKey(node);
    const reach = this.reaches.get(key) ?? { owned: false, unownedFrom: new Set<string>() };
    this.reaches.set(key, reach);
    if (context.settingsOwner || context.overlayOwner) reach.owned = true;
    else reach.unownedFrom.add(this.currentEntry);
  }

  private visit(node: ts.Node, context: Context, functionName?: string): void {
    const name = functionName ?? enclosingFunctionName(node);
    if (isSkippedSubtree(node)) return;
    if (ts.isIdentifier(node)) {
      // A function or component used as a value (an alias, `memo(X)`, a callback, a call) runs
      // or renders from here, so it's walked with this context.
      if (!isDeclarationName(node)) this.followReference(node, context);
      return;
    }
    if (ts.isJsxElement(node)) {
      const childContext = this.checkElement(node.openingElement, context, name, node);
      this.visitAttributes(node.openingElement, context, name);
      for (const child of node.children) this.visit(child, childContext, name);
      return;
    }
    if (ts.isJsxSelfClosingElement(node)) {
      this.checkElement(node, context, name);
      this.visitAttributes(node, context, name);
      return;
    }
    if (ts.isCallExpression(node) && this.checkCall(node, context, name)) return;
    if (ts.isObjectLiteralExpression(node)) this.checkEntryLiteral(node, name);
    // A subtree gated by an overlay getter whose narrowing is proven empty here never renders
    // under this owner: in `a && getter && b`, nothing after the getter runs; in a ternary on
    // such a chain, only its other arm does. The gate itself is walked as usual.
    const isAnd = (expression: ts.Expression): expression is ts.BinaryExpression =>
      ts.isBinaryExpression(expression) &&
      expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken;
    const conjuncts = (expression: ts.Expression): ts.Expression[] => {
      const current = unwrapExpression(expression);
      return isAnd(current) ? [...conjuncts(current.left), ...conjuncts(current.right)] : [current];
    };
    const emptyGate = (operands: ts.Expression[]) =>
      operands.findIndex((operand) => this.overlayGetter(operand, context)?.kind === "empty");
    if (ts.isBinaryExpression(node) && isAnd(node) && !isAnd(node.parent as ts.Expression)) {
      const operands = conjuncts(node);
      const gate = emptyGate(operands);
      if (gate !== -1) {
        for (const operand of operands.slice(0, gate + 1)) this.visit(operand, context, name);
        return;
      }
    }
    if (ts.isConditionalExpression(node)) {
      const operands = conjuncts(node.condition);
      const gate = emptyGate(operands);
      if (gate !== -1) {
        for (const operand of operands.slice(0, gate + 1)) this.visit(operand, context, name);
        this.visit(node.whenFalse, context, name);
        return;
      }
    }
    ts.forEachChild(node, (child) => this.visit(child, context, name));
  }

  private visitAttributes(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    context: Context,
    name: string,
  ) {
    for (const attribute of element.attributes.properties) {
      if (ts.isJsxSpreadAttribute(attribute)) {
        this.visit(attribute.expression, context, name);
        continue;
      }
      const initializer = attribute.initializer;
      if (!initializer) continue;
      if (ts.isJsxExpression(initializer) && initializer.expression) {
        this.visit(initializer.expression, context, name);
      } else {
        this.visit(initializer, context, name);
      }
    }
  }

  /** Checks a call; true when it has walked the call's arguments itself. */
  private checkCall(call: ts.CallExpression, context: Context, name: string): boolean {
    if (call.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const [specifier] = call.arguments;
      if (specifier && ts.isStringLiteral(specifier)) this.queueModule(call, specifier.text);
      return false;
    }
    const resolved = this.resolve(call.expression);
    // A content area's element map is walked once the whole walk knows whether a content
    // site uses it: then only plain links, and otherwise as ordinary code.
    if (resolved?.name === "settingsContentComponents" && isIn(resolved.declaration, SEARCH_DIR)) {
      this.visit(call.expression, context, name);
      this.pendingContentMaps.push({ call, context, name });
      return true;
    }
    if (resolved?.name === "settingsEntry" && isIn(resolved.declaration, SEARCH_DIR)) {
      if (!isIn(call, CATALOG_DIR)) {
        this.report(call, name, "settingsEntry", "Entries are declared in the Settings catalog.");
      }
      return false;
    }
    // Inside a data region, its one `.map(...)` over its data is the row template; another map
    // there gets no row exemption.
    if (context.region && call === context.region.template) {
      const rowContext: Context = { ...context, region: { ...context.region, inRow: true } };
      this.visit(call.expression, context, name);
      for (const argument of call.arguments) this.visit(argument, rowContext, name);
      return true;
    }
    // A map inside a row renders rows of its own, which need their own region.
    if (context.region?.inRow && isMapCall(call)) {
      const outside: Context = { ...context, region: undefined };
      this.visit(call.expression, context, name);
      for (const argument of call.arguments) this.visit(argument, outside, name);
      return true;
    }
    return false;
  }

  /**
   * Follows a function, component or value the app's own code defines, through aliases and
   * wrappers such as `memo(...)`, with the context it is used in.
   */
  private followReference(identifier: ts.Identifier, context: Context) {
    const resolved = this.resolveIdentifier(identifier);
    if (!resolved || !this.traversable(resolved.declaration)) return;
    this.followDeclaration(resolved.declaration, resolved.name, context);
  }

  /**
   * Walks what a declaration defines: a function or class, or a variable's initializer, so an
   * alias (`const A = B`) or a wrapper (`memo(B)`) leads on to `B`.
   */
  private followDeclaration(declaration: ts.Declaration, name: string, context: Context) {
    if (
      ts.isFunctionDeclaration(declaration) ||
      ts.isFunctionExpression(declaration) ||
      ts.isArrowFunction(declaration) ||
      ts.isClassDeclaration(declaration)
    ) {
      this.visitFunction(declaration, name, context);
    } else if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
      this.visitFunction(declaration.initializer, name, context);
    }
  }

  /** An identifier's symbol, including the value a `{ name }` shorthand property refers to. */
  private resolveIdentifier(identifier: ts.Identifier) {
    if (ts.isShorthandPropertyAssignment(identifier.parent)) {
      const symbol = this.checker.getShorthandAssignmentValueSymbol(identifier.parent);
      const declaration = symbol?.declarations?.[0];
      return symbol && declaration ? { declaration, name: symbol.name } : undefined;
    }
    return this.resolve(identifier);
  }

  private queueModule(from: ts.Node, specifier: string) {
    const resolved = ts.resolveModuleName(
      specifier,
      from.getSourceFile().fileName,
      this.input.program.getCompilerOptions(),
      ts.sys,
    ).resolvedModule;
    if (resolved && !resolved.isExternalLibraryImport) {
      this.pendingRoots.push(path.resolve(resolved.resolvedFileName));
    }
  }

  private checkElement(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    outer: Context,
    name: string,
    full?: ts.JsxElement,
  ): Context {
    const provided = this.overlayProvider(element, outer, name);
    if (provided) return provided;
    const tag = this.classify(element.tagName);
    const tagText = element.tagName.getText();
    const context = this.regionRowContext(element, tag, outer, name, tagText);
    // Without a <summary>, the browser gives a <details> a "Details" control of its own, in
    // a content area's markdown too.
    if (tag.kind === "intrinsic" && tag.name === "details" && !hasSummaryChild(full)) {
      this.report(
        element,
        name,
        tagText,
        "A disclosure needs its own <summary>, which is its control.",
      );
    }
    if (context.content) return this.checkContentElement(element, tag, context, name, tagText);
    // A content map's allowance is for its content sites only; rendered anywhere else, it's refused.
    const components = attributeExpression(element, "components");
    if (
      components &&
      this.contentComponentsCall(components) &&
      !this.trustedMarkdown.has(element)
    ) {
      this.report(
        element,
        name,
        tagText,
        "A content map renders only as the markdown of its validated content area.",
      );
    }
    // A later owner spread replaces an earlier one's names and owner, so an element takes one.
    const ownerSpreads = element.attributes.properties.filter(
      (attribute) => ts.isJsxSpreadAttribute(attribute) && this.isOwnerSpread(attribute.expression),
    );
    if (ownerSpreads.length > 1) {
      this.report(
        element,
        name,
        tagText,
        "An element takes one owner spread: settingsControlProps, settingsOverlayProps or one ownership getter.",
      );
      return context;
    }
    this.checkRuntimeChoice(element, tag, context, name, tagText);
    // A hidden file input bound to a part is that part's plumbing, not a control of its own.
    if (this.checkFileInput(element, tag, context, name, tagText)) return context;
    const ownerProps =
      this.spreadOwner(element, "settingsControlProps") ??
      this.ownershipOwner(element, "control", context, name, tagText) ??
      this.overlayPartsOwner(element, "control", context, name, tagText);

    if (tag.kind === "search" && tag.name === "SettingsDataRegion") {
      return this.checkRegion(element, full, context, name);
    }
    if (tag.kind === "search" && tag.name === "SettingsContent") {
      this.checkContent(element, full, context, name);
      return context;
    }
    if (tag.kind === "search" && tag.name === "SettingsOwner") {
      return this.checkSettingsOwner(element, context, name);
    }
    if (tag.kind === "search" && tag.name === "SettingsOverlayOwner") {
      return this.checkOverlayOwner(element, context, name);
    }
    if (tag.kind === "search" && tag.name === "SettingsItem") {
      const entryExpression = attributeExpression(element, "entry");
      const owner = entryExpression ? this.entryOwner(entryExpression) : undefined;
      // Another entry's row inside a region is that entry's, not the region's.
      const sameRow = context.region !== undefined && sameRuntimeEntry(owner, context.region.owner);
      return { ...context, item: owner, region: sameRow ? context.region : undefined };
    }
    if (
      (tag.kind === "ui" && UI_OVERLAYS.has(tag.name)) ||
      (tag.kind === "intrinsic" && tag.name === "datalist")
    ) {
      const rowOverlay = this.rowOverlayContext(element, context, name, tagText);
      if (rowOverlay) return rowOverlay;
      const overlayOwner =
        this.spreadOwner(element, "settingsOverlayProps") ??
        this.ownershipOwner(element, "overlay", context, name, tagText) ??
        this.overlayPartsOwner(element, "overlay", context, name, tagText);
      if (!overlayOwner) {
        this.report(
          element,
          name,
          tagText,
          "Overlay content needs settingsOverlayProps(entry, part) to link it to its opener.",
        );
        return { region: context.region };
      }
      const overlayEntry = overlayOwner.entry;
      // An overlay opens from exactly one control, so it names one declared part.
      if (overlayEntry && (overlayOwner.candidates?.length ?? 1) > 1) {
        this.report(
          element,
          name,
          tagText,
          "Overlay content names the one part that opens it, not a choice of parts.",
        );
        return { region: context.region };
      }
      const part = overlayOwner.part;
      const undeclared = ownerEntries(overlayOwner).find(
        (member) => part !== undefined && member.parts?.[part] === undefined,
      );
      if (undeclared) {
        this.report(
          element,
          name,
          tagText,
          `"${part}" is not a declared part of ${undeclared.id}.`,
        );
        return { region: context.region };
      }
      // A shared dialog's controls still read the overlay owner and the hooks around them.
      return {
        overlay: overlayOwner,
        region: context.region,
        scopes: context.scopes,
        overlayOwner: context.overlayOwner,
      };
    }

    const control = this.controlKind(element, tag);
    // A radio group with owner props is its row's control; its radios are that control's choices.
    // Only a DOM element whose role is written out: no other spread can change it afterwards.
    const isRadioGroup =
      tag.kind === "intrinsic" &&
      stringAttribute(element, "role") === "radiogroup" &&
      element.attributes.properties.every(
        (attribute) =>
          !ts.isJsxSpreadAttribute(attribute) || this.isOwnerSpread(attribute.expression),
      );
    if (ownerProps && control === "none" && isRadioGroup) {
      this.checkPart(element, ownerProps, name, tagText);
      return { ownedControl: ownerProps, region: context.region };
    }
    // A tab list with owner props is its part's control, and its tabs are that part's choices.
    // Only ui/'s TabsList without a role of its own, or a DOM element whose role is written out,
    // and with no other spread that could change its role or tabs afterwards.
    const looksLikeTabList =
      (tag.kind === "ui" && tag.name === "TabsList") ||
      (tag.kind === "intrinsic" && stringAttribute(element, "role") === "tablist");
    if (looksLikeTabList) {
      // The nearest tab list decides: an unowned one inside an owned one owns its own tabs, none.
      if (!ownerProps) return { ...context, ownedTabList: undefined };
      const onlyOwnerSpreads = element.attributes.properties.every(
        (attribute) =>
          !ts.isJsxSpreadAttribute(attribute) || this.isOwnerSpread(attribute.expression),
      );
      // ui/'s TabsList forwards its props, so a role, or asChild with a child's role, replaces
      // its tablist role.
      const keepsItsRole =
        tag.kind !== "ui" || (!hasAttribute(element, "role") && !hasAttribute(element, "asChild"));
      if (!onlyOwnerSpreads || !keepsItsRole) {
        this.report(
          element,
          name,
          tagText,
          'An owned tab list is ui/\'s TabsList or a role="tablist" element, with no other spread, no role of its own and no asChild.',
        );
        return { ...context, ownedTabList: undefined };
      }
      this.checkPart(element, ownerProps, name, tagText);
      return { ownedControl: ownerProps, ownedTabList: true, region: context.region };
    }
    if (tag.kind === "component") {
      if (ownerProps) {
        this.report(element, name, tagText, "Control props go on a control, not on a component.");
      }
      this.followComponent(tag, context);
      return context;
    }
    if (control === "none") return context;

    // A tab is a choice of the owned tab list it sits in, whatever owner props it's given. Owner
    // props on anything that may be a tab are refused, whatever else it carries.
    if (control === "tab" || this.mayBeTab(element, tag)) {
      if (ownerProps) {
        this.report(
          element,
          name,
          tagText,
          "A tab is a choice of its owned tab list; owner props go on the list.",
        );
        return context;
      }
      if (control === "tab" && context.ownedTabList) return context;
    }
    if (ownerProps) {
      this.checkPart(element, ownerProps, name, tagText);
      return { ownedControl: ownerProps, region: context.region };
    }
    if (control === "option" && (context.ownedControl || context.overlay)) return context;
    if (this.isRowControl(element, tag, context)) {
      // A row field's own options, such as a select's, are that field's values.
      return stringAttribute(element, "data-settings-row-field") === undefined
        ? context
        : { ...context, ownedControl: context.region?.owner ?? {} };
    }
    if (this.isDialogAction(element, tag, context)) return context;
    this.report(
      element,
      name,
      tagText,
      "A Settings control needs settingsControlProps(entry, part) from a catalog entry.",
    );
    return context;
  }

  private followComponent(tag: Extract<TagKind, { kind: "component" }>, context: Context) {
    this.followDeclaration(tag.declaration, nameOf(tag.declaration) ?? "<component>", context);
  }

  private checkRegion(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    full: ts.JsxElement | undefined,
    context: Context,
    name: string,
  ): Context {
    const entryExpression = attributeExpression(element, "entry");
    const owner = entryExpression ? this.entryOwner(entryExpression) : undefined;
    const regionName = stringAttribute(element, "region");
    const members = ownerEntries(owner);
    const declarations = members.map((member) =>
      regionName === undefined ? undefined : member.regions?.[regionName],
    );
    // A family's members declare one region alike, so its rows are checked the same everywhere.
    const declared = declarations.every(
      (declaration) => JSON.stringify(declaration) === JSON.stringify(declarations[0]),
    )
      ? declarations[0]
      : undefined;
    if (!owner?.entry || !declared) {
      this.report(
        element,
        name,
        "SettingsDataRegion",
        "A data region names its entry and one of that entry's declared regions, as string literals.",
      );
    } else {
      // Each concrete entry's region has one site, whichever lookup or subset reaches it.
      const keys = members.map((member) => `${member.id}:${regionName}`);
      const taken = keys.find((key) => {
        const site = this.regionSites.get(key);
        return site !== undefined && site !== element;
      });
      if (taken) {
        this.report(
          element,
          name,
          "SettingsDataRegion",
          `${taken} is already rendered by another data region; each region has one site.`,
        );
      } else {
        for (const key of keys) this.regionSites.set(key, element);
      }
    }
    // Its actions and fields come from the catalog only, so a site can't add its own.
    if (
      element.attributes.properties.some(
        (attribute) =>
          ts.isJsxSpreadAttribute(attribute) ||
          ["rowActions", "rowFields"].includes(attribute.name.getText()),
      )
    ) {
      this.report(
        element,
        name,
        "SettingsDataRegion",
        "A data region's row actions and fields are declared in its catalog entry, not passed here.",
      );
    }
    const templates = full ? rowTemplates(full) : [];
    if (templates.length !== 1) {
      this.report(
        element,
        name,
        "SettingsDataRegion",
        "A data region wraps exactly one row template: a .map(...) over its data.",
      );
    }
    return {
      region: {
        owner,
        actions: new Set(Object.keys(declared?.rowActions ?? {})),
        dataNamed: new Set(
          Object.entries(declared?.rowActions ?? {}).flatMap(([action, actionName]) =>
            typeof actionName === "string" ? [] : [action],
          ),
        ),
        fields: new Set(Object.keys(declared?.rowFields ?? {})),
        inRow: false,
        template: templates.length === 1 ? templates[0] : undefined,
      },
      // A region in a dialog, such as its list of choices, is still in that dialog.
      overlay: context.overlay,
      scopes: context.scopes,
    };
  }

  /**
   * Overlay content spreading `settingsRowOverlayProps(entry, region, action)`: the dialog a
   * region's row action opens, whose controls are only that dialog's declared actions and fields.
   * Undefined for overlay content without that spread.
   */
  private rowOverlayContext(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    context: Context,
    name: string,
    tagText: string,
  ): Context | undefined {
    const call = element.attributes.properties
      .filter(ts.isJsxSpreadAttribute)
      .map((attribute) => this.helperCall(attribute.expression, "settingsRowOverlayProps"))
      .find((found) => found !== undefined);
    if (!call) return undefined;
    const [entryArgument, regionArgument, actionArgument] = call.arguments;
    const owner = entryArgument ? this.entryOwner(entryArgument) : undefined;
    const literal = (argument: ts.Expression | undefined) => {
      const current = argument ? unwrapExpression(argument) : undefined;
      return current && ts.isStringLiteralLike(current) ? current.text : undefined;
    };
    const regionName = literal(regionArgument);
    const action = literal(actionArgument);
    const declarations = ownerEntries(owner).map((member) => {
      const region = regionName === undefined ? undefined : member.regions?.[regionName];
      return action !== undefined && region?.rowActions?.[action] !== undefined
        ? region.rowOverlays?.[action]
        : undefined;
    });
    // A family's members declare the dialog alike, so its controls are checked the same.
    const declared = declarations.every(
      (declaration) => JSON.stringify(declaration) === JSON.stringify(declarations[0]),
    )
      ? declarations[0]
      : undefined;
    if (!owner?.entry || !declared) {
      this.report(
        element,
        name,
        tagText,
        "A row's dialog names its entry, one of its regions and a row action that declares the dialog, as string literals.",
      );
      return { region: undefined, scopes: context.scopes };
    }
    return {
      region: {
        owner,
        actions: new Set(Object.keys(declared.rowActions ?? {})),
        dataNamed: new Set(),
        fields: new Set(Object.keys(declared.rowFields ?? {})),
        inRow: true,
      },
      scopes: context.scopes,
    };
  }

  /**
   * Each content map: an object literal of renderers. Only one a valid content site renders
   * through its markdown gets the links-only allowance; any other is walked as ordinary code.
   */
  private checkContentMaps() {
    for (const { call, context, name } of this.pendingContentMaps) {
      const [map] = call.arguments;
      const isRendererMap =
        call.arguments.length === 1 &&
        map !== undefined &&
        ts.isObjectLiteralExpression(map) &&
        map.properties.every(
          (property) =>
            ts.isPropertyAssignment(property) &&
            (ts.isArrowFunction(property.initializer) ||
              ts.isFunctionExpression(property.initializer)),
        );
      if (!isRendererMap) {
        this.report(
          call,
          name,
          "settingsContentComponents",
          "settingsContentComponents takes one object literal of element renderers.",
        );
      }
      const trusted = isRendererMap && this.trustedContentMaps.has(call);
      if (trusted) this.checkContentMapUses(call);
      for (const argument of call.arguments) {
        this.visit(argument, trusted ? { content: true } : context, name);
      }
    }
  }

  /**
   * A trusted map is used only as the `components` of a validated content area's markdown. Any
   * other value read of its const, under any name it's imported or re-exported as, through an
   * alias, a spread, a shorthand or an options object, is refused, since it could render the
   * map's links outside the area. Type queries read nothing at runtime, so they're skipped.
   */
  private checkContentMapUses(call: ts.CallExpression) {
    // The const the call initializes, through the same wrappers the trust check accepts.
    let initializer: ts.Node = call;
    while (
      ts.isParenthesizedExpression(initializer.parent) ||
      ts.isAsExpression(initializer.parent) ||
      ts.isSatisfiesExpression(initializer.parent)
    ) {
      initializer = initializer.parent;
    }
    const declaration = initializer.parent;
    if (!ts.isVariableDeclaration(declaration) || declaration.initializer !== initializer) return;
    for (const source of this.filesReaching(declaration.getSourceFile())) {
      for (const read of this.constReads(source).get(declaration) ?? []) {
        if (this.isTrustedMarkdownUse(read)) continue;
        this.report(
          read,
          enclosingFunctionName(read),
          read.text,
          "A content map is used only as the components of its validated content area's markdown.",
        );
      }
    }
  }

  /**
   * The files that can read a binding of `target`: itself, and every file importing from one of
   * them (statically, through a re-export, or by `import()`), until none is new.
   */
  private filesReaching(target: ts.SourceFile): Set<ts.SourceFile> {
    if (!this.importersCache) {
      const importers = new Map<ts.SourceFile, Set<ts.SourceFile>>();
      const add = (source: ts.SourceFile, specifier: ts.Expression | undefined) => {
        const imported =
          specifier && this.checker.getSymbolAtLocation(specifier)?.declarations?.[0];
        if (imported && ts.isSourceFile(imported)) {
          importers.set(imported, (importers.get(imported) ?? new Set()).add(source));
        }
      };
      for (const source of this.ownSources()) {
        for (const statement of source.statements) {
          if (ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement)) {
            add(source, statement.moduleSpecifier);
          } else if (
            ts.isImportEqualsDeclaration(statement) &&
            ts.isExternalModuleReference(statement.moduleReference)
          ) {
            add(source, statement.moduleReference.expression);
          }
        }
        // A dynamic import can sit anywhere, so only a file that has one is walked for it.
        if (!/\bimport\s*\(/.test(source.text)) continue;
        const walk = (node: ts.Node) => {
          if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
            add(source, node.arguments[0]);
          }
          ts.forEachChild(node, walk);
        };
        walk(source);
      }
      this.importersCache = importers;
    }
    const reaching = new Set([target]);
    for (const file of reaching) {
      for (const importer of this.importersCache.get(file) ?? []) reaching.add(importer);
    }
    return reaching;
  }

  private ownSources(): ts.SourceFile[] {
    return this.input.program
      .getSourceFiles()
      .filter(
        (source) =>
          !source.isDeclarationFile && !normalize(source.fileName).includes("/node_modules/"),
      );
  }

  /**
   * Every value read in `source` of each const, keyed by the const each identifier resolves to
   * rather than how it's spelled: renamed, default or namespace imports and re-exports all count.
   * A module namespace read whole (`{...ns}`, `ns[key]`) reads every const it exports.
   */
  private constReads(source: ts.SourceFile): Map<ts.Declaration, ts.Identifier[]> {
    const cached = this.constReadsCache.get(source);
    if (cached) return cached;
    const reads = new Map<ts.Declaration, ts.Identifier[]>();
    const record = (declaration: ts.Declaration | undefined, read: ts.Identifier) => {
      if (!declaration || !ts.isVariableDeclaration(declaration) || declaration.name === read) {
        return;
      }
      reads.set(declaration, [...(reads.get(declaration) ?? []), read]);
    };
    const walk = (node: ts.Node) => {
      if (isSkippedSubtree(node) || ts.isTypeQueryNode(node)) return;
      if (ts.isIdentifier(node)) this.recordConstRead(node, record);
      ts.forEachChild(node, walk);
    };
    walk(source);
    this.constReadsCache.set(source, reads);
    return reads;
  }

  private recordConstRead(
    node: ts.Identifier,
    record: (declaration: ts.Declaration | undefined, read: ts.Identifier) => void,
  ) {
    const parent = node.parent;
    if (isDeclarationName(node) && !ts.isShorthandPropertyAssignment(parent)) return;
    // `export default MAP` only renames it; its importers' reads are recorded in turn.
    if (ts.isExportAssignment(parent) && parent.expression === node) return;
    const namespace = this.moduleNamespace(node);
    if (namespace) {
      // `ns.MAP` is recorded at its name; `ns[key]` or a whole `ns` reads what it may export.
      if (ts.isPropertyAccessExpression(parent) && parent.expression === node) return;
      const key =
        ts.isElementAccessExpression(parent) &&
        parent.expression === node &&
        ts.isStringLiteralLike(parent.argumentExpression)
          ? parent.argumentExpression.text
          : undefined;
      if (key !== undefined) {
        record(this.moduleExport(namespace, key), node);
      } else {
        for (const member of this.checker.getExportsOfModule(namespace)) {
          record(this.aliasTarget(member), node);
        }
      }
      return;
    }
    record(this.resolveIdentifier(node)?.declaration, node);
  }

  /** The module an identifier names as a namespace (`import * as ns`), if it does. */
  private moduleNamespace(identifier: ts.Identifier): ts.Symbol | undefined {
    const symbol = this.checker.getSymbolAtLocation(identifier);
    const target = symbol && this.aliasSymbol(symbol);
    return target && target.flags & ts.SymbolFlags.ValueModule ? target : undefined;
  }

  private moduleExport(namespace: ts.Symbol, name: string): ts.Declaration | undefined {
    const member = this.checker.tryGetMemberInModuleExports(name, namespace);
    return member && this.aliasTarget(member);
  }

  private aliasSymbol(symbol: ts.Symbol): ts.Symbol {
    return symbol.flags & ts.SymbolFlags.Alias ? this.checker.getAliasedSymbol(symbol) : symbol;
  }

  private aliasTarget(symbol: ts.Symbol): ts.Declaration | undefined {
    return this.aliasSymbol(symbol).declarations?.[0];
  }

  /** Whether `identifier` is exactly `components={MAP}` on a validated content area's markdown. */
  private isTrustedMarkdownUse(identifier: ts.Identifier): boolean {
    const expression = identifier.parent;
    if (!ts.isJsxExpression(expression) || expression.expression !== identifier) return false;
    const attribute = expression.parent;
    if (!ts.isJsxAttribute(attribute) || attribute.name.getText() !== "components") return false;
    return this.trustedMarkdown.has(attribute.parent.parent);
  }

  /**
   * An element a content area's markdown renders: a plain `<a>`, with no spread or handler of its
   * own, or something that isn't a control at all.
   */
  private checkContentElement(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
    name: string,
    tagText: string,
  ): Context {
    const plain = element.attributes.properties.every(
      (attribute) => ts.isJsxAttribute(attribute) && !/^on[A-Z]/.test(attribute.name.getText()),
    );
    if (tag.kind === "intrinsic" && tag.name === "a") {
      if (!plain) {
        this.report(
          element,
          name,
          tagText,
          "A content link is a plain <a href>, with no spread or handler of its own.",
        );
      }
      return context;
    }
    if (tag.kind !== "intrinsic" || !plain || this.controlKind(element, tag) !== "none") {
      this.report(element, name, tagText, "A content area's markdown renders only plain links.");
    }
    return context;
  }

  /**
   * `<SettingsContent entry area>`: one declared area of its entry, at one site, holding only
   * trusted markdown: `ReactMarkdown` with `skipHtml`, no plugins that could add HTML, and an
   * element map made by `settingsContentComponents`.
   */
  private checkContent(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    full: ts.JsxElement | undefined,
    context: Context,
    name: string,
  ) {
    const entryExpression = attributeExpression(element, "entry");
    const owner = entryExpression ? this.entryOwner(entryExpression) : undefined;
    const area = stringAttribute(element, "area");
    const members = ownerEntries(owner);
    const declared =
      area !== undefined &&
      members.length > 0 &&
      members.every((member) => member.contentAreas?.includes(area));
    if (!declared || element.attributes.properties.some(ts.isJsxSpreadAttribute)) {
      this.report(
        element,
        name,
        "SettingsContent",
        "A content area names its entry and one of that entry's declared content areas, as string literals.",
      );
      return;
    }
    const keys = members.map((member) => `content:${member.id}:${area}`);
    if (keys.some((key) => this.regionSites.has(key) && this.regionSites.get(key) !== element)) {
      this.report(element, name, "SettingsContent", "Each content area has one site.");
      return;
    }
    for (const key of keys) this.regionSites.set(key, element);
    // An area is part of its entry's row, so it sits under that row's SettingsItem.
    if (!sameRuntimeEntry(owner, context.item)) {
      this.report(element, name, "SettingsContent", "A content area sits in its own entry's row.");
      return;
    }
    const children = full ? full.children.filter((child) => !isBlankText(child)) : [];
    const child = children.length === 1 ? children[0] : undefined;
    const markdown =
      child && ts.isJsxElement(child)
        ? child.openingElement
        : child && ts.isJsxSelfClosingElement(child)
          ? child
          : undefined;
    const resolved = markdown ? this.resolve(markdown.tagName as ts.Expression) : undefined;
    const isMarkdown =
      resolved !== undefined &&
      normalize(resolved.declaration.getSourceFile().fileName).includes("/react-markdown/");
    const components = markdown ? attributeExpression(markdown, "components") : undefined;
    const trusted =
      isMarkdown &&
      markdown !== undefined &&
      hasAttribute(markdown, "skipHtml") &&
      attributeExpression(markdown, "skipHtml") === undefined &&
      !["rehypePlugins", "urlTransform", "allowElement", "unwrapDisallowed"].some((attribute) =>
        hasAttribute(markdown, attribute),
      ) &&
      !markdown.attributes.properties.some(ts.isJsxSpreadAttribute) &&
      components !== undefined &&
      this.contentComponentsCall(components) !== undefined;
    if (trusted) {
      this.trustedContentMaps.add(this.contentComponentsCall(components)!);
      this.trustedMarkdown.add(markdown);
    } else {
      this.report(
        element,
        name,
        "SettingsContent",
        "A content area holds one ReactMarkdown with skipHtml, no HTML plugins, and components made by settingsContentComponents.",
      );
    }
  }

  /** The `settingsContentComponents(...)` call a const `expression` is initialized by, if so. */
  private contentComponentsCall(expression: ts.Expression): ts.CallExpression | undefined {
    const current = unwrapExpression(expression);
    if (!ts.isIdentifier(current)) return undefined;
    const declaration = this.resolve(current)?.declaration;
    if (
      !declaration ||
      !ts.isVariableDeclaration(declaration) ||
      !declaration.initializer ||
      (ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const) === 0
    ) {
      return undefined;
    }
    const initializer = unwrapExpression(declaration.initializer);
    if (!ts.isCallExpression(initializer)) return undefined;
    const resolved = this.resolve(initializer.expression);
    return resolved?.name === "settingsContentComponents" && isIn(resolved.declaration, SEARCH_DIR)
      ? initializer
      : undefined;
  }

  /**
   * An element marked `data-settings-region-row` is one row of its region: a DOM element in the
   * row template, not inside another row. Inside it, the region's data-named actions may sit.
   */
  private regionRowContext(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
    name: string,
    tagText: string,
  ): Context {
    if (!hasAttribute(element, "data-settings-region-row")) return context;
    const region = context.region;
    if (tag.kind !== "intrinsic" || !region?.inRow || region.inRegionRow) {
      this.report(
        element,
        name,
        tagText,
        "A region row is a DOM element in its data region's row template, not inside another row.",
      );
      return context;
    }
    return { ...context, region: { ...region, inRegionRow: true } };
  }

  private isRowControl(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
  ): boolean {
    const region = context.region;
    if (!region?.inRow) return false;
    const field = stringAttribute(element, "data-settings-row-field");
    if (field !== undefined) {
      if (!region.fields.has(field) || !ROW_FIELD_CONTROLS.has(tagName(tag))) return false;
      const type = stringAttribute(element, "type");
      return type === undefined || !NON_FIELD_INPUT_TYPES.has(type);
    }
    const action = stringAttribute(element, "data-settings-row-action");
    if (action === undefined || !region.actions.has(action)) return false;
    // A data-named action sits in a marked region row, where the audit counts one per row.
    if (region.dataNamed.has(action) && !region.inRegionRow) return false;
    return ROW_ACTION_CONTROLS.has(tagName(tag));
  }

  private isDialogAction(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
  ): boolean {
    const owner = context.overlay;
    const action = stringAttribute(element, "data-settings-dialog-action");
    if (!owner?.entry || action === undefined) return false;
    const name = tagName(tag);
    if (name !== "button" && name !== "Button") return false;
    return ownerEntries(owner).every(
      (member) => member.overlays?.[owner.part ?? ""]?.dialogActions?.includes(action) ?? false,
    );
  }

  private checkPart(element: ts.Node, owner: Owner, name: string, tagText: string) {
    for (const entry of ownerEntries(owner)) {
      this.checkMemberPart(element, { ...owner, entry, family: undefined }, name, tagText);
    }
  }

  private checkMemberPart(element: ts.Node, owner: Owner, name: string, tagText: string) {
    const entry = owner.entry;
    if (!entry || owner.part === undefined) return;
    const candidates = owner.candidates ?? [owner.part];
    const undeclared = candidates.filter((part) => entry.parts?.[part] === undefined);
    for (const part of undeclared) {
      this.report(element, name, tagText, `"${part}" is not a declared part of ${entry.id}.`);
    }
    if (undeclared.length > 0) return;
    // Any templated candidate makes the control templated: it may render as that part.
    const templated = candidates.find((part) => entry.nameTemplates?.[part] !== undefined);
    if (templated === undefined) return;
    // A templated part is named only by its formatter, given the runtime value.
    if (!ts.isJsxOpeningElement(element) && !ts.isJsxSelfClosingElement(element)) return;
    if (!this.passesRuntimeName(element)) {
      this.report(
        element,
        name,
        tagText,
        `${entry.id}:${templated} is named by a template, so its props need the runtime value.`,
      );
    }
    for (const attribute of NAMING_ATTRIBUTES) {
      if (hasAttribute(element, attribute)) {
        this.report(
          element,
          name,
          tagText,
          `${entry.id}:${templated} is named by its template; drop the separate ${attribute}.`,
        );
      }
    }
    for (const attribute of element.attributes.properties) {
      if (!ts.isJsxSpreadAttribute(attribute) || this.isOwnerSpread(attribute.expression)) continue;
      const naming = this.spreadNaming(attribute.expression);
      if (naming === undefined) {
        this.report(
          element,
          name,
          tagText,
          `${entry.id}:${templated} is named by its template; spread only object literals beside it, so they can be checked.`,
        );
      } else if (naming.length > 0) {
        this.report(
          element,
          name,
          tagText,
          `${entry.id}:${templated} is named by its template; drop the separate ${naming.join(", ")}.`,
        );
      }
    }
  }

  /**
   * A `settingsRuntimeChoiceProps(entry, "part")` spread: only on an option of that part's own
   * owned overlay, when the part declares `runtimeChoices`.
   */
  private checkRuntimeChoice(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
    name: string,
    tagText: string,
  ) {
    for (const attribute of element.attributes.properties) {
      if (!ts.isJsxSpreadAttribute(attribute)) continue;
      const call = this.helperCall(attribute.expression, "settingsRuntimeChoiceProps");
      if (!call) continue;
      const [entryArgument, partArgument] = call.arguments;
      const owner = entryArgument ? this.entryOwner(entryArgument) : undefined;
      const part =
        partArgument && ts.isStringLiteralLike(partArgument) ? partArgument.text : undefined;
      const overlay = context.overlay;
      const valid =
        owner?.entry !== undefined &&
        part !== undefined &&
        sameRuntimeEntry(owner, overlay) &&
        overlay?.part === part &&
        ownerEntries(owner).every(
          (member) => member.overlays?.[part]?.runtimeChoices !== undefined,
        ) &&
        this.controlKind(element, tag) === "option";
      if (!valid) {
        this.report(
          element,
          name,
          tagText,
          "A runtime choice is an option of the overlay its marker names, a part declaring runtimeChoices, by a string literal.",
        );
      }
    }
  }

  /**
   * A `settingsFileInputProps(entry, "part")` spread, true when there is one: only on an
   * intrinsic `<input type="file" hidden>`, binding a part the entry declares.
   */
  private checkFileInput(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
    context: Context,
    name: string,
    tagText: string,
  ): boolean {
    const call = element.attributes.properties
      .filter(ts.isJsxSpreadAttribute)
      .map((attribute) => this.helperCall(attribute.expression, "settingsFileInputProps"))
      .find((found) => found !== undefined);
    if (!call) return false;
    const [entryArgument, partArgument] = call.arguments;
    const owner = entryArgument ? this.entryOwner(entryArgument) : undefined;
    const part =
      partArgument && ts.isStringLiteralLike(partArgument) ? partArgument.text : undefined;
    // Every attribute is written out and can't expose it: no other spread, no tabIndex of its
    // own, no class or style that could show it. So its final props are the ones checked here.
    const attributesKnown = element.attributes.properties.every((attribute) =>
      ts.isJsxSpreadAttribute(attribute)
        ? this.helperCall(attribute.expression, "settingsFileInputProps") === call
        : FILE_INPUT_ATTRIBUTES.has(attribute.name.getText()),
    );
    const isHiddenFileInput =
      tag.kind === "intrinsic" &&
      tag.name === "input" &&
      attributesKnown &&
      stringAttribute(element, "type") === "file" &&
      hasAttribute(element, "hidden") &&
      attributeExpression(element, "hidden") === undefined;
    const inItsRow = sameRuntimeEntry(owner, context.item);
    if (
      !isHiddenFileInput ||
      !inItsRow ||
      part === undefined ||
      ownerEntries(owner).some((member) => member.parts?.[part] === undefined)
    ) {
      this.report(
        element,
        name,
        tagText,
        'settingsFileInputProps binds a hidden <input type="file">, with only its own written attributes, to a declared part of the row it sits in, named by a string literal.',
      );
    }
    return true;
  }

  /** The owner's own spread: a control or overlay helper, or a `useSettingsOwnership` getter. */
  private isOwnerSpread(expression: ts.Expression): boolean {
    return (
      this.helperCall(expression, "settingsControlProps") !== undefined ||
      this.helperCall(expression, "settingsOverlayProps") !== undefined ||
      this.helperCall(expression, "settingsRowOverlayProps") !== undefined ||
      this.ownershipCall(expression, "control") !== undefined ||
      this.ownershipCall(expression, "overlay") !== undefined ||
      this.overlayPartsCall(expression, "control") !== undefined ||
      this.overlayPartsCall(expression, "overlay") !== undefined
    );
  }

  /**
   * The naming attributes another spread sets: it's an object literal, written inline or bound
   * to a const. Undefined when it can't be read, so it might set any of them.
   */
  private spreadNaming(expression: ts.Expression): string[] | undefined {
    const literal = this.objectLiteral(expression);
    if (!literal) return undefined;
    const names: string[] = [];
    for (const property of literal.properties) {
      if (ts.isSpreadAssignment(property)) {
        const nested = this.spreadNaming(property.expression);
        if (nested === undefined) return undefined;
        names.push(...nested);
        continue;
      }
      const key = property.name && propertyKey(property.name);
      if (key === undefined) return undefined;
      if (NAMING_ATTRIBUTES.includes(key)) names.push(key);
    }
    return names;
  }

  /** An object literal written here, or the initializer of the const it's bound to. */
  private objectLiteral(expression: ts.Expression): ts.ObjectLiteralExpression | undefined {
    const current = unwrapExpression(expression);
    if (ts.isObjectLiteralExpression(current)) return current;
    if (!ts.isIdentifier(current)) return undefined;
    const declaration = this.resolve(current)?.declaration;
    if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      return undefined;
    }
    if (!(ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const)) return undefined;
    const initializer = unwrapExpression(declaration.initializer);
    return ts.isObjectLiteralExpression(initializer) ? initializer : undefined;
  }

  /**
   * Whether the owner spread passes a runtime name, `{ runtime }`: the helper's third argument,
   * or a getter's `name` option.
   */
  private passesRuntimeName(element: ts.JsxOpeningElement | ts.JsxSelfClosingElement): boolean {
    for (const attribute of element.attributes.properties) {
      if (!ts.isJsxSpreadAttribute(attribute)) continue;
      const call = this.helperCall(attribute.expression, "settingsControlProps");
      if (call) return isRuntimeName(call.arguments[2]);
      // A getter's options: the ownership getter's first argument, an overlay getter's second.
      const getter = this.ownershipCall(attribute.expression, "control");
      const overlayGetter = this.overlayPartsCall(attribute.expression, "control");
      const options = getter
        ? getter.call.arguments[0]
        : overlayGetter
          ? overlayGetter.call.arguments[1]
          : undefined;
      if (getter || overlayGetter) {
        const literal = options && unwrapExpression(options);
        if (!literal || !ts.isObjectLiteralExpression(literal)) return false;
        return isRuntimeName(propertyValue(literal, "name"));
      }
    }
    return false;
  }

  /** `<SettingsOwner entry part>`: a catalog entry and, if any, one of its declared parts. */
  private checkSettingsOwner(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    context: Context,
    name: string,
  ): Context {
    const entryExpression = attributeExpression(element, "entry");
    const owner = entryExpression ? this.entryOwner(entryExpression) : undefined;
    if (element.attributes.properties.some(ts.isJsxSpreadAttribute)) {
      this.report(
        element,
        name,
        "SettingsOwner",
        "A SettingsOwner takes its entry and part as attributes, not a spread.",
      );
      return context;
    }
    const partExpression = attributeExpression(element, "part");
    const part = partExpression ? this.constantString(partExpression) : undefined;
    if (partExpression && part === undefined) {
      this.report(
        element,
        name,
        "SettingsOwner",
        "A SettingsOwner names its part as a string literal or a string constant.",
      );
      return context;
    }
    if (!owner?.entry) {
      this.report(element, name, "SettingsOwner", "A SettingsOwner names a catalog entry.");
      return context;
    }
    const undeclared = ownerEntries(owner).find(
      (member) => part !== undefined && member.parts?.[part] === undefined,
    );
    if (undeclared) {
      this.report(
        element,
        name,
        "SettingsOwner",
        `"${part}" is not a declared part of ${undeclared.id}.`,
      );
      return context;
    }
    return { ...context, settingsOwner: { ...owner, part, candidates: undefined } };
  }

  /**
   * `<SettingsOverlayOwner entry opener>`: a catalog entry, or a family's, each of whose
   * candidates declares the overlay its opener part opens. The dialog inside names that
   * overlay's parts through `useSettingsOverlayParts()`.
   */
  private checkOverlayOwner(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    context: Context,
    name: string,
  ): Context {
    const tagText = "SettingsOverlayOwner";
    if (element.attributes.properties.some(ts.isJsxSpreadAttribute)) {
      this.report(
        element,
        name,
        tagText,
        "A SettingsOverlayOwner takes its entry and opener as attributes, not a spread.",
      );
      return context;
    }
    const entryExpression = attributeExpression(element, "entry");
    const owner = entryExpression ? this.entryOwner(entryExpression) : undefined;
    const openerExpression = attributeExpression(element, "opener");
    const opener = openerExpression ? this.constantString(openerExpression) : undefined;
    if (!owner?.entry || opener === undefined) {
      this.report(
        element,
        name,
        tagText,
        "A SettingsOverlayOwner names a catalog entry and its opener part as a string literal.",
      );
      return context;
    }
    const entries = ownerEntries(owner);
    const missing = entries.find((entry) => entry.overlays?.[opener] === undefined);
    if (missing) {
      this.report(element, name, tagText, `${missing.id} declares no overlay for "${opener}".`);
      return context;
    }
    return { ...context, overlayOwner: { owner, opener, entries } };
  }

  /** The family member each family entry was built for. */
  private memberOf(entry: SettingsEntry): string | undefined {
    if (!this.entryMembers) {
      this.entryMembers = new Map();
      for (const members of this.input.families?.values() ?? []) {
        for (const [member, each] of members) this.entryMembers.set(each, member);
      }
    }
    return this.entryMembers.get(entry);
  }

  /** The strings of a literal array, written here or bound to a const (`as const` and all). */
  private literalStrings(expression: ts.Expression): string[] | undefined {
    let current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      const declaration = this.resolve(current)?.declaration;
      if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
        return undefined;
      }
      if (!(ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const)) return undefined;
      current = unwrapExpression(declaration.initializer);
    }
    if (!ts.isArrayLiteralExpression(current)) return undefined;
    const values = current.elements.map((element) =>
      ts.isStringLiteralLike(element) ? element.text : undefined,
    );
    return values.every((value) => value !== undefined) ? (values as string[]) : undefined;
  }

  /**
   * The overlay getter a const binding holds where `context` reads it: `useSettingsOverlayParts()`
   * reads the owner its component rendered under; `parts.within(members, opener?)` narrows one.
   */
  private overlayGetter(expression: ts.Expression, context: Context): OverlayGetter | undefined {
    const current = withoutNonNull(expression);
    if (!ts.isIdentifier(current)) return undefined;
    const declaration = this.resolve(current)?.declaration;
    if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      return undefined;
    }
    if (!(ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const)) return undefined;
    const initializer = unwrapExpression(declaration.initializer);
    if (!ts.isCallExpression(initializer)) return undefined;
    const resolved = this.resolve(initializer.expression);
    if (resolved?.name === "useSettingsOverlayParts" && isIn(resolved.declaration, SEARCH_DIR)) {
      const owner = hookScope(initializer, context.scopes ?? [])?.overlayOwner;
      return owner ? { kind: "owned", owner } : { kind: "unowned" };
    }
    const callee = initializer.expression;
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== "within") return undefined;
    const base = this.overlayGetter(callee.expression, context);
    if (!base || base.kind !== "owned") return base;
    const [membersArgument, openerArgument, ...rest] = initializer.arguments;
    const members = membersArgument ? this.literalStrings(membersArgument) : undefined;
    const opener = openerArgument ? this.constantString(openerArgument) : undefined;
    if (!members || (openerArgument && opener === undefined) || rest.length > 0) {
      return {
        kind: "unknown",
        reason: "within() takes a literal member list and, if any, a literal opener.",
      };
    }
    const owner = base.owner;
    const memberships = owner.entries.map((entry) => this.memberOf(entry));
    if (memberships.some((member) => member === undefined)) {
      return {
        kind: "unknown",
        reason: "within() narrows a family's members; this owner isn't one.",
      };
    }
    const entries = owner.entries.filter((_entry, index) => members.includes(memberships[index]!));
    if ((opener !== undefined && opener !== owner.opener) || entries.length === 0) {
      return { kind: "empty" };
    }
    return { kind: "owned", owner: { ...owner, entries } };
  }

  /** A spread of an overlay getter's `control(...)` or `overlay()`, with the getter it calls. */
  private overlayPartsCall(
    expression: ts.Expression,
    getter: "control" | "overlay",
  ): { call: ts.CallExpression; getter: ts.Expression } | undefined {
    if (!ts.isCallExpression(expression)) return undefined;
    const callee = expression.expression;
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== getter) return undefined;
    const target = withoutNonNull(callee.expression);
    if (!ts.isIdentifier(target)) return undefined;
    const declaration = this.resolve(target)?.declaration;
    if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      return undefined;
    }
    const initializer = unwrapExpression(declaration.initializer);
    if (!ts.isCallExpression(initializer)) return undefined;
    const resolved = this.resolve(initializer.expression);
    const isHook =
      resolved?.name === "useSettingsOverlayParts" && isIn(resolved.declaration, SEARCH_DIR);
    const isWithin =
      ts.isPropertyAccessExpression(initializer.expression) &&
      initializer.expression.name.text === "within";
    return isHook || isWithin ? { call: expression, getter: target } : undefined;
  }

  /** Why an overlay getter here can't own a control, or undefined when it can. */
  private overlayGetterProblem(getter: OverlayGetter): string | undefined {
    if (getter.kind === "unowned") {
      return "A shared dialog's control is owned only under a SettingsOverlayOwner at its Settings site.";
    }
    if (getter.kind === "empty") {
      return "This narrowing leaves the overlay no owner here; render its parts only behind that getter.";
    }
    if (getter.kind === "unknown") return getter.reason;
    return undefined;
  }

  /**
   * The owner an overlay getter's spread gives its element: one of the overlay's parts, each
   * candidate declaring it inside that overlay, or the overlay's own content.
   */
  private overlayPartsOwner(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    getter: "control" | "overlay",
    context: Context,
    name: string,
    tagText: string,
  ): Owner | undefined {
    const spread = element.attributes.properties
      .filter(ts.isJsxSpreadAttribute)
      .map((attribute) => this.overlayPartsCall(attribute.expression, getter))
      .find((call) => call !== undefined);
    if (!spread) return undefined;
    const resolved = this.overlayGetter(spread.getter, context);
    if (!resolved) return undefined;
    const problem = this.overlayGetterProblem(resolved);
    if (problem || resolved.kind !== "owned") {
      this.report(element, name, tagText, problem!);
      return undefined;
    }
    const { owner, opener, entries } = resolved.owner;
    const narrowed: Owner = { ...owner, entry: entries[0], family: entries, lookup: undefined };
    if (getter === "overlay") return { ...narrowed, part: opener === "" ? undefined : opener };
    // A part named by a literal, a constant, or a finite union of literals, every one checked.
    const partArgument = spread.call.arguments[0];
    const constant = partArgument ? this.constantString(partArgument) : undefined;
    const candidates =
      constant !== undefined
        ? [constant]
        : partArgument
          ? this.partCandidates(partArgument)
          : undefined;
    if (!candidates) {
      this.report(
        element,
        name,
        tagText,
        "An overlay part is named by a string literal, a string constant, or a finite union of them.",
      );
      return undefined;
    }
    for (const entry of entries) {
      for (const part of candidates) {
        const presence = entry.presence?.[part];
        if (entry.parts?.[part] === undefined) {
          this.report(element, name, tagText, `"${part}" is not a declared part of ${entry.id}.`);
          return undefined;
        }
        if (!presence || !("overlay" in presence) || presence.overlay !== opener) {
          this.report(
            element,
            name,
            tagText,
            `${entry.id}:${part} isn't a part of its "${opener}" overlay.`,
          );
          return undefined;
        }
      }
    }
    return {
      ...narrowed,
      part: candidates[0],
      candidates: candidates.length > 1 ? candidates : undefined,
    };
  }

  /** `<parts.Provider>`: lends a getter's owner, as narrowed, to the components inside. */
  private overlayProvider(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    context: Context,
    name: string,
  ): Context | undefined {
    const providerTag = element.tagName;
    if (!ts.isPropertyAccessExpression(providerTag) || providerTag.name.text !== "Provider")
      return undefined;
    const resolved = this.overlayGetter(providerTag.expression, context);
    if (!resolved) return undefined;
    if (resolved.kind === "owned") return { ...context, overlayOwner: resolved.owner };
    // Rendered with no Settings owner, its children read none, and report their own controls.
    if (resolved.kind === "unowned") return { ...context, overlayOwner: undefined };
    this.report(element, name, providerTag.getText(), this.overlayGetterProblem(resolved)!);
    return { ...context, overlayOwner: undefined };
  }

  /** A string literal, or a const initialized to one. */
  private constantString(expression: ts.Expression): string | undefined {
    const current = unwrapExpression(expression);
    if (ts.isStringLiteralLike(current)) return current.text;
    if (!ts.isIdentifier(current)) return undefined;
    const declaration = this.resolve(current)?.declaration;
    if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      return undefined;
    }
    if (!(ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const)) return undefined;
    const initializer = unwrapExpression(declaration.initializer);
    return ts.isStringLiteralLike(initializer) ? initializer.text : undefined;
  }

  /**
   * A spread of a `useSettingsOwnership()` getter, `owned.control(...)` or `owned.overlay()`,
   * with the hook call whose owner it reads.
   */
  private ownershipCall(
    expression: ts.Expression,
    getter: "control" | "overlay",
  ): { call: ts.CallExpression; hook: ts.CallExpression } | undefined {
    if (!ts.isCallExpression(expression)) return undefined;
    const callee = expression.expression;
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== getter) return undefined;
    if (!ts.isIdentifier(callee.expression)) return undefined;
    const declaration = this.resolve(callee.expression)?.declaration;
    if (!declaration || !ts.isVariableDeclaration(declaration) || !declaration.initializer) {
      return undefined;
    }
    const init = declaration.initializer;
    if (!ts.isCallExpression(init)) return undefined;
    const resolved = this.resolve(init.expression);
    return resolved?.name === "useSettingsOwnership" && isIn(resolved.declaration, SEARCH_DIR)
      ? { call: expression, hook: init }
      : undefined;
  }

  /**
   * The owner a getter spread gives its element: the SettingsOwner the component calling
   * `useSettingsOwnership()` was rendered under, as React reads it. One that component renders
   * itself doesn't count. Reached from Settings without one, the control is reported as unowned.
   */
  private ownershipOwner(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    getter: "control" | "overlay",
    context: Context,
    name: string,
    tagText: string,
  ): Owner | undefined {
    const spread = element.attributes.properties
      .filter(ts.isJsxSpreadAttribute)
      .map((attribute) => this.ownershipCall(attribute.expression, getter))
      .find((call) => call !== undefined);
    if (!spread) return undefined;
    const owner = hookScope(spread.hook, context.scopes ?? [])?.owner;
    if (owner) return owner;
    if (context.settingsOwner) {
      this.report(
        element,
        name,
        tagText,
        "useSettingsOwnership() runs above this SettingsOwner, so it doesn't see it; call it in a component rendered inside.",
      );
      return undefined;
    }
    this.report(
      element,
      name,
      tagText,
      "A shared component's control is owned only under a SettingsOwner at its Settings site.",
    );
    // Reported here, so the generic unowned-control report at this element is a duplicate.
    return undefined;
  }

  /**
   * Whether an element may render as a tab: ui/'s TabsTrigger in any form, or an element whose
   * role is written as "tab" or isn't a literal. A spread beside it can't make it not one.
   */
  private mayBeTab(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
  ): boolean {
    if (tag.kind === "ui" && tag.name === "TabsTrigger") return true;
    if (!hasAttribute(element, "role")) return false;
    const role = stringAttribute(element, "role");
    return role === undefined || role === "tab";
  }

  private controlKind(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    tag: TagKind,
  ): "control" | "option" | "tab" | "none" {
    const role = stringAttribute(element, "role");
    if (role !== undefined && OPTION_ROLES.has(role)) return "option";
    // A tab: ui/'s TabsTrigger as it is, or an element whose role is written out as one. A role,
    // asChild or a spread could make it something else, so with any of them it's a plain control.
    // Owner props name it without changing its role, so they keep it a tab, to be placed.
    const spreads = element.attributes.properties.some(
      (attribute) =>
        ts.isJsxSpreadAttribute(attribute) && !this.isOwnerSpread(attribute.expression),
    );
    if (role === "tab" && !spreads) return "tab";
    if (
      tag.kind === "ui" &&
      tag.name === "TabsTrigger" &&
      !spreads &&
      !hasAttribute(element, "role") &&
      !hasAttribute(element, "asChild")
    ) {
      return "tab";
    }
    if (tag.kind === "intrinsic") {
      if (tag.name === "option") return "option";
      if (tag.name === "input" && stringAttribute(element, "type") === "radio") return "option";
      if (INTRINSIC_CONTROLS.has(tag.name)) return "control";
      if (tag.name === "a" && hasAttribute(element, "href")) return "control";
    }
    if (tag.kind === "ui") {
      if (UI_OPTIONS.has(tag.name)) return "option";
      // A Button that is always asChild renders its child as the control, which is checked itself.
      if (tag.name === "Button" && isLiteralTrue(element, "asChild")) return "none";
      if (UI_CONTROLS.has(tag.name)) return "control";
      if (UI_TRIGGERS.has(tag.name) && !hasAttribute(element, "asChild")) return "control";
    }
    if (tag.kind === "link") return "control";
    if (hasAttribute(element, "role")) {
      if (role === undefined || INTERACTIVE_ROLES.has(role)) return "control";
    }
    const tabIndex = attributeExpression(element, "tabIndex");
    if (tabIndex && !isMinusOne(tabIndex)) return "control";
    return "none";
  }

  private classify(tagNameNode: ts.JsxTagNameExpression): TagKind {
    if (ts.isIdentifier(tagNameNode) && /^[a-z]/.test(tagNameNode.text)) {
      return { kind: "intrinsic", name: tagNameNode.text };
    }
    const resolved = this.resolve(tagNameNode as ts.Expression);
    if (!resolved) return { kind: "external" };
    const file = resolved.declaration.getSourceFile().fileName;
    if (file.includes(`${path.sep}node_modules${path.sep}`) || file.includes("/node_modules/")) {
      return { kind: "external" };
    }
    if (isIn(resolved.declaration, UI_DIR)) return { kind: "ui", name: resolved.name };
    if (NOT_TRAVERSED.some((prefix) => normalize(file).includes(prefix))) {
      return normalize(file).includes(`${path.sep}lib${path.sep}routing`) &&
        resolved.name === "Link"
        ? { kind: "link" }
        : { kind: "external" };
    }
    if (
      isIn(resolved.declaration, SEARCH_DIR) &&
      /^Settings(DataRegion|Owner|OverlayOwner|Item|Content)$/.test(resolved.name)
    ) {
      return { kind: "search", name: resolved.name, declaration: resolved.declaration };
    }
    const key = declarationKey(resolved.declaration, resolved.name);
    return {
      kind: "component",
      key,
      declaration: resolved.declaration,
    };
  }

  private traversable(declaration: ts.Declaration): boolean {
    const file = normalize(declaration.getSourceFile().fileName);
    if (file.includes(`${path.sep}node_modules${path.sep}`)) return false;
    if (!file.startsWith(normalize(path.join(this.input.webRoot, "src")))) return false;
    if (isIn(declaration, UI_DIR)) return false;
    return !NOT_TRAVERSED.some((prefix) => file.includes(prefix));
  }

  /** The symbol an expression names, through import aliases and re-exports. */
  private resolve(expression: ts.Node): { declaration: ts.Declaration; name: string } | undefined {
    let symbol = this.checker.getSymbolAtLocation(
      ts.isPropertyAccessExpression(expression) ? expression.name : expression,
    );
    if (!symbol) return undefined;
    if (symbol.flags & ts.SymbolFlags.Alias) symbol = this.checker.getAliasedSymbol(symbol);
    const declaration = symbol.declarations?.[0];
    if (!declaration) return undefined;
    return { declaration, name: symbol.name };
  }

  /** The owner a `{...settingsControlProps(entry, part)}` spread (or a const holding one) names. */
  private spreadOwner(
    element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
    helper: "settingsControlProps" | "settingsOverlayProps",
  ): Owner | undefined {
    for (const attribute of element.attributes.properties) {
      if (!ts.isJsxSpreadAttribute(attribute)) continue;
      const call = this.helperCall(attribute.expression, helper);
      if (!call) continue;
      const [entryArgument, partArgument] = call.arguments;
      const owner = entryArgument ? this.entryOwner(entryArgument) : undefined;
      if (!owner) continue;
      if (!partArgument) return owner;
      const candidates = this.partCandidates(partArgument);
      return candidates
        ? { ...owner, part: candidates[0], candidates }
        : { ...owner, part: partName(partArgument) };
    }
    return undefined;
  }

  /**
   * The parts a part argument may be: a string literal, or an expression typed as one string
   * literal or a finite union of them. Anything broader (string, any, unknown, never, or a
   * union with another type) is undefined, so it's reported as an undeclared part.
   */
  private partCandidates(expression: ts.Expression): string[] | undefined {
    const current = unwrapExpression(expression);
    if (ts.isStringLiteralLike(current)) return [current.text];
    const type = this.checker.getTypeAtLocation(current);
    const members = type.isUnion() ? type.types : [type];
    const parts: string[] = [];
    for (const member of members) {
      if (!member.isStringLiteral()) return undefined;
      parts.push(member.value);
    }
    return parts.length > 0 ? parts : undefined;
  }

  private helperCall(expression: ts.Expression, helper: string): ts.CallExpression | undefined {
    let candidate: ts.Expression = expression;
    if (ts.isIdentifier(candidate)) {
      const declaration = this.resolve(candidate)?.declaration;
      if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer) {
        candidate = declaration.initializer;
      }
    }
    if (!ts.isCallExpression(candidate)) return undefined;
    const resolved = this.resolve(candidate.expression);
    return resolved?.name === helper && isIn(resolved.declaration, SEARCH_DIR)
      ? candidate
      : undefined;
  }

  /**
   * The entry an expression passes: a catalog export, known statically, or a value typed as an
   * entry handed in as a parameter (a row component's `entry` prop). Literals outside the
   * catalog are rejected where they're written.
   */
  private entryOwner(expression: ts.Expression): Owner | undefined {
    if (ts.isObjectLiteralExpression(expression)) return undefined;
    const family = this.familyLookup(expression);
    if (family) {
      return family.entries.length === 1
        ? { entry: family.entries[0] }
        : { entry: family.entries[0], family: family.entries, lookup: family.lookup };
    }
    const resolved = this.resolve(expression);
    if (resolved && isIn(resolved.declaration, CATALOG_DIR)) {
      const entry = this.input.catalog.get(declarationKey(resolved.declaration, resolved.name));
      return entry ? { entry } : undefined;
    }
    // Only an entry handed in by the caller, such as a row component's `entry` prop, is dynamic.
    if (!this.isParameterValue(expression)) return undefined;
    const type = this.checker.getTypeAtLocation(expression);
    const isEntry = ["id", "section", "label", "description"].every(
      (property) => type.getProperty(property) !== undefined,
    );
    return isEntry ? {} : undefined;
  }

  /**
   * The entries a registered family's `FAMILY.entry(member)` may find: the one member a literal
   * names, the members a literal union allows, or every member; and the looked-up value.
   */
  private familyLookup(
    expression: ts.Expression,
  ): { entries: readonly SettingsEntry[]; lookup?: FamilyLookupValue } | undefined {
    let current = unwrapExpression(expression);
    // `const entry = FAMILY.entry(member)`: the alias is the lookup it's initialized to, and
    // every read of the alias is one snapshot of it.
    let snapshot: ts.VariableDeclaration | undefined;
    if (ts.isIdentifier(current)) {
      const declaration = this.resolve(current)?.declaration;
      if (
        declaration &&
        ts.isVariableDeclaration(declaration) &&
        declaration.initializer &&
        (ts.getCombinedNodeFlags(declaration) & ts.NodeFlags.Const) !== 0
      ) {
        snapshot = declaration;
        current = unwrapExpression(declaration.initializer);
      }
    }
    if (!ts.isCallExpression(current) || current.arguments.length !== 1) return undefined;
    const callee = current.expression;
    if (!ts.isPropertyAccessExpression(callee) || callee.name.text !== "entry") return undefined;
    const resolved = this.resolve(callee.expression);
    if (!resolved || !isIn(resolved.declaration, CATALOG_DIR)) return undefined;
    const members = this.input.families?.get(declarationKey(resolved.declaration, resolved.name));
    if (!members || members.size === 0) return undefined;
    const argument = current.arguments[0]!;
    const named = this.partCandidates(argument) ?? [...members.keys()];
    // A literal that isn't a member is a type error; here it simply owns nothing.
    if (named.length === 0 || named.some((member) => !members.has(member))) return undefined;
    const entries = named.map((member) => members.get(member)!);
    // The value read: unchanged everywhere it's read, or else this one snapshot of the lookup.
    const value = this.unchangingValue(argument);
    const key =
      value ??
      (snapshot
        ? `snapshot:${snapshot.getSourceFile().fileName}:${snapshot.getStart()}`
        : undefined);
    return {
      entries,
      lookup:
        key === undefined
          ? undefined
          : {
              key,
              entryByMember: new Map(named.map((member) => [member, members.get(member)!.id])),
            },
    };
  }

  /**
   * Identifies a value that can't change between two reads: a const, or a parameter or a
   * destructured binding of one (or of a const) that nothing in its function assigns. A
   * property read, a `let` and anything else are undefined, so placement checks fail closed.
   */
  private unchangingValue(expression: ts.Expression): string | undefined {
    const current = unwrapExpression(expression);
    if (!ts.isIdentifier(current)) return undefined;
    const declaration = this.resolve(current)?.declaration;
    if (!declaration) return undefined;
    let root: ts.Node = declaration;
    while (ts.isBindingElement(root)) root = root.parent.parent;
    const isConst =
      ts.isVariableDeclaration(root) && (ts.getCombinedNodeFlags(root) & ts.NodeFlags.Const) !== 0;
    if (!isConst && !ts.isParameter(root)) return undefined;
    if (!isConst && this.isAssigned(declaration)) return undefined;
    return `value:${declaration.getSourceFile().fileName}:${declaration.getStart()}`;
  }

  /**
   * Whether anything in the declaring function may write it: an assignment, `++`/`--`, a
   * destructuring assignment (shorthand included), or a `for…of`/`for…in` target, through any
   * parentheses. A read of the same name in a nested target counts, so unsupported shapes are
   * writes too.
   */
  private isAssigned(declaration: ts.Declaration): boolean {
    let scope: ts.Node = declaration;
    while (!ts.isFunctionLike(scope) && !ts.isSourceFile(scope)) scope = scope.parent;
    let assigned = false;
    const walk = (node: ts.Node) => {
      if (assigned) return;
      if (
        ts.isIdentifier(node) &&
        node !== (declaration as ts.NamedDeclaration).name &&
        this.resolveIdentifier(node)?.declaration === declaration &&
        isWriteTarget(node)
      ) {
        assigned = true;
        return;
      }
      ts.forEachChild(node, walk);
    };
    walk(scope);
    return assigned;
  }

  private isParameterValue(expression: ts.Expression): boolean {
    const base = ts.isPropertyAccessExpression(expression) ? expression.expression : expression;
    if (!ts.isIdentifier(base)) return false;
    const declaration = this.resolve(base)?.declaration;
    return (
      declaration !== undefined && (ts.isParameter(declaration) || ts.isBindingElement(declaration))
    );
  }

  /** An entry written outside the catalog would be searchable nowhere, so it isn't allowed. */
  private checkEntryLiteral(literal: ts.ObjectLiteralExpression, name: string) {
    if (isIn(literal, SEARCH_DIR)) return;
    const keys = new Set(
      literal.properties.flatMap((property) =>
        property.name && ts.isIdentifier(property.name) ? [property.name.text] : [],
      ),
    );
    if (["id", "section", "label", "description"].every((key) => keys.has(key))) {
      this.report(literal, name, "entry literal", "Entries are declared in the Settings catalog.");
    }
  }

  private report(node: ts.Node, functionName: string, element: string, reason: string) {
    const source = node.getSourceFile();
    const file = path.relative(this.input.webRoot, source.fileName).split(path.sep).join("/");
    const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
    const position = `${source.fileName}:${node.getStart()}`;
    if (this.violations.has(position)) return;
    this.violations.set(position, {
      site: `${file} › ${functionName} › ${element}`,
      file,
      line,
      reason,
    });
  }
}

function normalize(file: string): string {
  return path.normalize(file);
}

function isIn(node: ts.Node, directory: string): boolean {
  return normalize(node.getSourceFile().fileName).includes(directory);
}

function tagName(tag: TagKind): string {
  if (tag.kind === "intrinsic" || tag.kind === "ui") return tag.name;
  if (tag.kind === "link") return "Link";
  return "";
}

function topLevelFunctions(statement: ts.Statement): { name: string; node: ts.Node }[] {
  if (ts.isFunctionDeclaration(statement) && statement.name) {
    return [{ name: statement.name.text, node: statement }];
  }
  if (ts.isVariableStatement(statement)) {
    return statement.declarationList.declarations.flatMap((declaration) => {
      const body = functionNode(declaration);
      return body && ts.isIdentifier(declaration.name)
        ? [{ name: declaration.name.text, node: body }]
        : [];
    });
  }
  return [];
}

/** The function a declaration defines, through `memo(...)`, `forwardRef(...)` and the like. */
function functionNode(declaration: ts.Node): ts.Node | undefined {
  if (ts.isFunctionDeclaration(declaration) || ts.isFunctionExpression(declaration)) {
    return declaration;
  }
  if (ts.isArrowFunction(declaration)) return declaration;
  if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
    let initializer: ts.Expression = declaration.initializer;
    while (ts.isCallExpression(initializer) && initializer.arguments[0]) {
      const [first] = initializer.arguments;
      if (ts.isArrowFunction(first) || ts.isFunctionExpression(first)) return first;
      initializer = first;
    }
    if (ts.isArrowFunction(initializer) || ts.isFunctionExpression(initializer)) return initializer;
  }
  return undefined;
}

function nameOf(declaration: ts.Declaration): string | undefined {
  const named = declaration as ts.Declaration & { name?: ts.Node };
  return named.name && ts.isIdentifier(named.name) ? named.name.text : undefined;
}

function enclosingFunctionName(node: ts.Node): string {
  for (let current: ts.Node | undefined = node; current; current = current.parent) {
    if (ts.isFunctionDeclaration(current) && current.name) return current.name.text;
    if (ts.isVariableDeclaration(current) && ts.isIdentifier(current.name)) {
      return current.name.text;
    }
  }
  return "<module>";
}

/** Type positions and import or export statements name things without using them. */
function isSkippedSubtree(node: ts.Node): boolean {
  return (
    ts.isTypeNode(node) ||
    ts.isImportDeclaration(node) ||
    ts.isExportDeclaration(node) ||
    ts.isInterfaceDeclaration(node) ||
    ts.isTypeAliasDeclaration(node) ||
    ts.isJsxClosingElement(node)
  );
}

/** The name a declaration introduces, as opposed to a use of it. */
function isDeclarationName(identifier: ts.Identifier): boolean {
  const parent = identifier.parent;
  if (ts.isPropertyAccessExpression(parent) || ts.isShorthandPropertyAssignment(parent)) {
    return false;
  }
  if (ts.isJsxAttribute(parent)) return true;
  return "name" in parent && (parent as { name?: ts.Node }).name === identifier;
}

/** A `.map(...)` or `.flatMap(...)` call: one row per item. */
function isMapCall(call: ts.CallExpression): boolean {
  return (
    ts.isPropertyAccessExpression(call.expression) &&
    (call.expression.name.text === "map" || call.expression.name.text === "flatMap")
  );
}

/** The `.map(...)` calls in a region not nested inside another one's callback: its templates. */
function rowTemplates(node: ts.Node): ts.CallExpression[] {
  const found: ts.CallExpression[] = [];
  const walk = (child: ts.Node) => {
    if (ts.isCallExpression(child) && isMapCall(child)) {
      found.push(child);
      return;
    }
    ts.forEachChild(child, walk);
  };
  walk(node);
  return found;
}

function findAttribute(
  element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
  name: string,
): ts.JsxAttribute | undefined {
  return element.attributes.properties.find(
    (property): property is ts.JsxAttribute =>
      ts.isJsxAttribute(property) && property.name.getText() === name,
  );
}

function hasAttribute(element: ts.JsxOpeningElement | ts.JsxSelfClosingElement, name: string) {
  return findAttribute(element, name) !== undefined;
}

function attributeExpression(
  element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
  name: string,
): ts.Expression | undefined {
  const initializer = findAttribute(element, name)?.initializer;
  if (!initializer) return undefined;
  if (ts.isStringLiteral(initializer)) return initializer;
  return ts.isJsxExpression(initializer) ? initializer.expression : undefined;
}

function stringAttribute(
  element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
  name: string,
): string | undefined {
  const expression = attributeExpression(element, name);
  return expression && ts.isStringLiteralLike(expression) ? expression.text : undefined;
}

/** A part is a string literal; anything else can't be matched against the entry's parts. */
function partName(expression: ts.Expression): string {
  const current = unwrapExpression(expression);
  return ts.isStringLiteralLike(current) ? current.text : `<${current.getText()}>`;
}

const NAMING_ATTRIBUTES = ["aria-label", "aria-labelledby", "title"];

/** What a hidden file input may set beside its binding: nothing that could show or focus it. */
const FILE_INPUT_ATTRIBUTES = new Set([
  "type",
  "hidden",
  "accept",
  "multiple",
  "name",
  "ref",
  "onChange",
  "data-testid",
]);

/**
 * Whether `name` is always true where the element renders: its last word is a bare `name` or
 * `name={true}`. A spread after it may set it to anything, so it's no longer known true.
 */
function isLiteralTrue(
  element: ts.JsxOpeningElement | ts.JsxSelfClosingElement,
  name: string,
): boolean {
  let known = false;
  for (const property of element.attributes.properties) {
    if (ts.isJsxSpreadAttribute(property)) {
      known = false;
      continue;
    }
    if (property.name.getText() !== name) continue;
    const initializer = property.initializer;
    known =
      !initializer ||
      (ts.isJsxExpression(initializer) &&
        initializer.expression !== undefined &&
        unwrapExpression(initializer.expression).kind === ts.SyntaxKind.TrueKeyword);
  }
  return known;
}

function unwrapExpression(expression: ts.Expression): ts.Expression {
  let current = expression;
  while (
    ts.isAsExpression(current) ||
    ts.isParenthesizedExpression(current) ||
    ts.isSatisfiesExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}

function propertyKey(name: ts.PropertyName): string | undefined {
  if (ts.isIdentifier(name) || ts.isStringLiteralLike(name) || ts.isNumericLiteral(name)) {
    return name.text;
  }
  if (ts.isComputedPropertyName(name)) {
    const expression = unwrapExpression(name.expression);
    return ts.isStringLiteralLike(expression) ? expression.text : undefined;
  }
  return undefined;
}

/** A property's value in an object literal; a shorthand `{ name }` gives its identifier. */
function propertyValue(
  literal: ts.ObjectLiteralExpression,
  key: string,
): ts.Expression | undefined {
  for (const property of literal.properties) {
    if (!property.name || propertyKey(property.name) !== key) continue;
    if (ts.isPropertyAssignment(property)) return property.initializer;
    if (ts.isShorthandPropertyAssignment(property)) return property.name;
  }
  return undefined;
}

/** `{ runtime, variant? }` written out, with a runtime that isn't `undefined`. */
function isRuntimeName(expression: ts.Expression | undefined): boolean {
  if (!expression) return false;
  const literal = unwrapExpression(expression);
  if (!ts.isObjectLiteralExpression(literal)) return false;
  const runtime = propertyValue(literal, "runtime");
  if (!runtime) return false;
  const value = unwrapExpression(runtime);
  return !(ts.isIdentifier(value) && value.text === "undefined") && !ts.isVoidExpression(value);
}

/** An expression without the non-null assertions written after it, as in `getter!.control`. */
function withoutNonNull(expression: ts.Expression): ts.Expression {
  let current = unwrapExpression(expression);
  while (ts.isNonNullExpression(current)) current = unwrapExpression(current.expression);
  return current;
}

/** The innermost walked function that contains the hook call: the one whose owner it reads. */
function hookScope(hook: ts.Node, scopes: readonly Scope[]): Scope | undefined {
  for (let index = scopes.length - 1; index >= 0; index--) {
    if (contains(scopes[index]!.node, hook)) return scopes[index];
  }
  return undefined;
}

/** Whether `outer` lexically contains `inner`, in the same file. */
function contains(outer: ts.Node, inner: ts.Node): boolean {
  return (
    outer.getSourceFile() === inner.getSourceFile() &&
    outer.pos <= inner.pos &&
    inner.end <= outer.end
  );
}

function isMinusOne(expression: ts.Expression): boolean {
  return (
    ts.isPrefixUnaryExpression(expression) &&
    expression.operator === ts.SyntaxKind.MinusToken &&
    ts.isNumericLiteral(expression.operand) &&
    expression.operand.text === "1"
  );
}

function isBlankText(node: ts.JsxChild): boolean {
  return ts.isJsxText(node) && node.containsOnlyTriviaWhiteSpaces;
}

function contextSignature(context: Context): string {
  const owner = (value?: Owner) =>
    value
      ? `${value.family?.map((member) => member.id).join("|") ?? value.entry?.id ?? "?"}~${value.lookup?.key ?? ""}=${[...(value.lookup?.entryByMember ?? [])].join(",")}:${value.part ?? ""}`
      : "";
  const region = context.region
    ? `${owner(context.region.owner)}|${[...context.region.actions].join(",")}|${[...context.region.fields].join(",")}|${context.region.inRow}|${context.region.inRegionRow ?? false}|${context.region.template?.pos ?? ""}`
    : "";
  const overlayOwner = (value?: OverlayOwnerContext) =>
    value
      ? `${owner(value.owner)}@${value.opener}[${value.entries.map((entry) => entry.id).join("|")}]`
      : "";
  // Only owned scopes change what a hook reads, so unowned ones don't split the walk.
  const scopes = (context.scopes ?? [])
    .filter((scope) => scope.owner || scope.overlayOwner)
    .map((scope) => `${scope.node.pos}=${owner(scope.owner)}+${overlayOwner(scope.overlayOwner)}`)
    .join(",");
  return `${owner(context.ownedControl)}/${context.ownedTabList ?? false}/${context.content ?? false}/${owner(context.overlay)}/${region}/${owner(context.settingsOwner)}/${scopes}/${owner(context.item)}/${overlayOwner(context.overlayOwner)}`;
}

/**
 * Whether an identifier is written where it stands: the target, through parentheses and any
 * destructuring pattern, of an assignment, a `for…of`/`for…in` head, or `++`/`--`.
 */
function isWriteTarget(identifier: ts.Identifier): boolean {
  let target: ts.Node = identifier;
  // Climb out of parentheses and destructuring patterns to the whole assignment target.
  for (;;) {
    const parent = target.parent;
    if (
      ts.isParenthesizedExpression(parent) ||
      ts.isObjectLiteralExpression(parent) ||
      ts.isArrayLiteralExpression(parent) ||
      ts.isShorthandPropertyAssignment(parent) ||
      ts.isSpreadElement(parent) ||
      ts.isSpreadAssignment(parent) ||
      (ts.isPropertyAssignment(parent) && parent.initializer === target) ||
      // A default in a pattern, `{ member = fallback } = value`: the left side is the target.
      (ts.isBinaryExpression(parent) &&
        parent.left === target &&
        parent.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
        (ts.isObjectLiteralExpression(parent.parent) || ts.isArrayLiteralExpression(parent.parent)))
    ) {
      target = parent;
      continue;
    }
    break;
  }
  const parent = target.parent;
  if (ts.isBinaryExpression(parent) && parent.left === target) {
    return (
      parent.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
      parent.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    );
  }
  if (
    (ts.isForOfStatement(parent) || ts.isForInStatement(parent)) &&
    parent.initializer === target
  ) {
    return true;
  }
  if (ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) {
    return (
      parent.operator === ts.SyntaxKind.PlusPlusToken ||
      parent.operator === ts.SyntaxKind.MinusMinusToken
    );
  }
  return false;
}

/** Each entry an owner may be: a family lookup's members, or its one entry. */
function ownerEntries(owner: Owner | undefined): readonly SettingsEntry[] {
  if (!owner) return [];
  return owner.family ?? (owner.entry ? [owner.entry] : []);
}

/**
 * Whether two owners are provably the same entry when the page renders: one known entry each,
 * the same one; or lookups of the same value, which find the same member's one entry. Anything
 * else fails closed, including two lookups of different values over the same members.
 */
function sameRuntimeEntry(first: Owner | undefined, second: Owner | undefined): boolean {
  if (!first?.entry || !second?.entry) return false;
  if (!first.family && !second.family) return first.entry.id === second.entry.id;
  if (!first.family || !second.family || !first.lookup || !second.lookup) return false;
  if (first.lookup.key !== second.lookup.key) return false;
  // The same value through two families finds one entry only if they map every member alike.
  const firstMap = first.lookup.entryByMember;
  const secondMap = second.lookup.entryByMember;
  return (
    firstMap.size === secondMap.size &&
    [...firstMap].every(([member, id]) => secondMap.get(member) === id)
  );
}

function isEntryShaped(value: unknown): value is SettingsEntry {
  return typeof value === "object" && value !== null && "id" in value && "section" in value;
}

/**
 * The catalog the guard accepts: entries exported from the catalog modules that search also
 * lists. An entry declared but missing from the searchable list owns nothing, and is reported.
 */
export function catalogFromModules(
  modules: Readonly<Record<string, Readonly<Record<string, unknown>>>>,
  searchable: readonly SettingsEntry[],
): {
  catalog: Map<string, SettingsEntry>;
  families: Map<string, ReadonlyMap<string, SettingsEntry>>;
  unlisted: string[];
} {
  const listed = new Set(searchable);
  const catalog = new Map<string, SettingsEntry>();
  const families = new Map<string, ReadonlyMap<string, SettingsEntry>>();
  const unlisted: string[] = [];
  for (const [file, module] of Object.entries(modules)) {
    for (const [name, value] of Object.entries(module)) {
      if (isSettingsEntryFamily(value)) {
        families.set(
          `${file}#${name}`,
          new Map(value.members.map((member, index) => [member, value.entries[index]!])),
        );
        value.members.forEach((member, index) => {
          if (!listed.has(value.entries[index]!)) unlisted.push(`${file}#${name}(${member})`);
        });
        continue;
      }
      if (!isEntryShaped(value)) continue;
      if (listed.has(value)) catalog.set(`${file}#${name}`, value);
      else unlisted.push(`${file}#${name}`);
    }
  }
  return { catalog, families, unlisted };
}

/**
 * Whether a <details> has a <summary> among its own children: directly, in a fragment, or in a
 * conditional expression, but never inside another element.
 */
function hasSummaryChild(details: ts.JsxElement | undefined): boolean {
  if (!details) return false;
  const isSummary = (node: ts.Node): boolean => {
    if (ts.isJsxElement(node)) return node.openingElement.tagName.getText() === "summary";
    if (ts.isJsxSelfClosingElement(node)) return node.tagName.getText() === "summary";
    if (ts.isJsxFragment(node)) return node.children.some(isSummary);
    if (ts.isJsxExpression(node))
      return node.expression !== undefined && isSummary(node.expression);
    if (ts.isJsxText(node)) return false;
    // A conditional, logical or parenthesized child: look through it to the elements it gives.
    let found = false;
    ts.forEachChild(node, (child) => {
      found ||= isSummary(child);
    });
    return found;
  };
  return details.children.some(isSummary);
}
