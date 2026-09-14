import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangleIcon, RotateCcwIcon, XIcon } from "lucide-react";
import {
  ACTION_CATALOG,
  ACTIONS_BY_ID,
  KEYBINDING_MODES,
  contextsMayOverlap,
  formatKeybinding,
  getKeybindingSnapshot,
  isMacKeyboardPlatform,
  isReservedEscapeSequence,
  keybindingEnvironmentExpression,
  logicalKeyForCode,
  parseKeybinding,
  replaceAllUserKeybindings,
  resetAllUserKeybindings,
  resetUserKeybindingRule,
  resolveEffectiveKeymap,
  serializeKeybinding,
  setUserKeybindingCandidate,
  unbindDefaultKeybinding,
  useKeybindingSnapshot,
  type ActionId,
  type JsonValue,
  type KeybindingConflict,
  type KeybindingMode,
  type KeybindingMutationResult,
  type KeybindingRule,
  type UserKeybindingRule,
} from "@/actions";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsEmbedded } from "@/lib/embedded";
import { isNativeShell } from "@/lib/nativeBridge";
import { KeybindingRecorder } from "./KeybindingRecorder";
import { KEYBINDING_MODE_LABELS, KeybindingSequence } from "./KeybindingSequence";

interface BindingPill {
  id: string;
  action: ActionId;
  mode: KeybindingMode;
  args?: JsonValue;
  sequence: KeybindingRule["sequence"] | null;
  defaultRule?: KeybindingRule;
  userRule?: UserKeybindingRule;
}

interface BindingGroup {
  action: ActionId;
  mode: KeybindingMode;
  args?: JsonValue;
  label?: string;
  pills: BindingPill[];
}

interface PendingBinding {
  rule: UserKeybindingRule;
  conflicts: readonly KeybindingConflict[];
  warnings: readonly string[];
}

const TEXT_ENTRY_KEYS = new Set([
  "Enter",
  "Tab",
  "Backspace",
  "Delete",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
  "PageUp",
  "PageDown",
]);

function actionTitle(action: string): string {
  return ACTIONS_BY_ID.get(action as ActionId)?.title ?? action;
}

function argsOf(rule: KeybindingRule | UserKeybindingRule): JsonValue | undefined {
  return "args" in rule ? (rule.args as JsonValue | undefined) : undefined;
}

function canonicalJson(value: JsonValue | undefined): string {
  if (value === undefined) return "";
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function groupId(value: Pick<BindingGroup, "action" | "mode" | "args">): string {
  return `${value.action}\u0000${value.mode}\u0000${canonicalJson(value.args)}`;
}

function variantLabel(action: ActionId, args: JsonValue | undefined): string | undefined {
  if (!args || typeof args !== "object" || Array.isArray(args)) return undefined;
  if (action === "session.action.openPinned" && typeof args.slot === "number") {
    return `Slot ${args.slot + 1}`;
  }
  if (action === "composer.action.acceptSuggestion") {
    return args.behavior === "attach" ? "Attach" : "Open or attach";
  }
  if (action === "terminal.action.sendSequence") return "Terminal input";
  return undefined;
}

function resultMessage(result: KeybindingMutationResult): string | null {
  if (result.ok) return null;
  switch (result.reason) {
    case "invalidRule":
      return "That shortcut is invalid.";
    case "unusableRule":
      return "This action cannot use that shortcut.";
    case "limitReached":
      return "The keybinding limit has been reached.";
    case "storageUnavailable":
      return "The shortcut could not be saved in this browser.";
  }
}

function IconAction({
  label,
  onClick,
  disabled = false,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="icon-sm"
          aria-label={label}
          disabled={disabled}
          onClick={onClick}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function lastRulesById(rules: readonly UserKeybindingRule[]): readonly UserKeybindingRule[] {
  const seen = new Set<string>();
  return [...rules]
    .reverse()
    .filter((rule) => {
      if (seen.has(rule.id)) return false;
      seen.add(rule.id);
      return true;
    })
    .reverse();
}

function safetyWarnings(rule: UserKeybindingRule): string[] {
  if (!rule.sequence) return [];
  const stroke = parseKeybinding(rule.sequence)[0];
  const key = stroke.key.kind === "key" ? stroke.key.value : logicalKeyForCode(stroke.key.value);
  if (!key) return [];
  const warnings: string[] = [];
  const protective = stroke.modifiers.some((modifier) => modifier !== "shift");
  if (!protective && (key === " " || [...key].length === 1 || TEXT_ENTRY_KEYS.has(key))) {
    warnings.push(`${rule.sequence} can intercept normal text editing in ${rule.mode} mode.`);
  }
  const primaryOnly =
    stroke.modifiers.length === 1 &&
    ["mod", "primary", "ctrl", "meta"].includes(stroke.modifiers[0]);
  if (
    ["F5", "F11", "F12"].includes(key) ||
    (primaryOnly && ["l", "n", "p", "q", "r", "t", "w"].includes(key))
  ) {
    warnings.push(`${rule.sequence} may be reserved by the browser or operating system.`);
  }
  return warnings;
}

export function KeybindingEditor() {
  const snapshot = useKeybindingSnapshot();
  const embedded = useIsEmbedded();
  const native = isNativeShell();
  const isMac = isMacKeyboardPlatform();
  const [query, setQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<"all" | KeybindingMode>("all");
  const [editing, setEditing] = useState<BindingPill | null>(null);
  const [pending, setPending] = useState<PendingBinding | null>(null);
  const [confirmResetAll, setConfirmResetAll] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restoreFocusPrefix = useRef<string | null>(null);

  const runtimeDefaults = useMemo(() => {
    const environment = keybindingEnvironmentExpression({
      isMac,
      isNativeShell: native,
      isEmbedded: embedded,
    });
    return snapshot.defaultRules.filter(
      (rule) =>
        !isReservedEscapeSequence(rule.sequence) && contextsMayOverlap(rule.when, environment),
    );
  }, [embedded, isMac, native, snapshot.defaultRules]);

  const groupsByAction = useMemo(() => {
    const groups = new Map<ActionId, BindingGroup[]>();
    const defaultsById = new Map(snapshot.defaultRules.map((rule) => [rule.id, rule]));
    const effectiveById = new Map(snapshot.effectiveRules.map((rule) => [rule.id, rule]));
    const userRules = lastRulesById(snapshot.userRules);
    const userById = new Map(userRules.map((rule) => [rule.id, rule]));

    for (const definition of ACTION_CATALOG) {
      const actionDefaults = runtimeDefaults.filter((rule) => rule.action === definition.id);
      if (
        actionDefaults.length === 0 &&
        snapshot.defaultRules.some((rule) => rule.action === definition.id)
      ) {
        continue;
      }
      const actionGroups = new Map<string, BindingGroup>();
      for (const defaultRule of actionDefaults) {
        const userRule = userById.get(defaultRule.id);
        const targeted =
          userRule?.action === defaultRule.action && userRule.mode === defaultRule.mode
            ? userRule
            : undefined;
        const effective = effectiveById.get(defaultRule.id);
        const group: BindingGroup = {
          action: definition.id,
          mode: defaultRule.mode,
          args: argsOf(defaultRule),
          label: variantLabel(definition.id, argsOf(defaultRule)),
          pills: [],
        };
        const id = groupId(group);
        const existing = actionGroups.get(id) ?? group;
        existing.pills.push({
          id: defaultRule.id,
          action: definition.id,
          mode: defaultRule.mode,
          args: argsOf(defaultRule),
          sequence:
            targeted?.sequence === null ? null : (effective?.sequence ?? defaultRule.sequence),
          defaultRule,
          userRule: targeted,
        });
        actionGroups.set(id, existing);
      }

      for (const userRule of userRules) {
        if (userRule.action !== definition.id || !userRule.sequence) continue;
        let sequence;
        try {
          sequence = parseKeybinding(userRule.sequence);
        } catch {
          continue;
        }
        if (isReservedEscapeSequence(sequence)) continue;
        const target = defaultsById.get(userRule.id);
        if (target?.action === userRule.action && target.mode === userRule.mode) continue;
        const effective = effectiveById.get(userRule.id);
        if (
          effective?.origin !== "user" ||
          effective.action !== userRule.action ||
          effective.mode !== userRule.mode
        )
          continue;
        const group: BindingGroup = {
          action: definition.id,
          mode: userRule.mode,
          args: argsOf(userRule),
          label: variantLabel(definition.id, argsOf(userRule)),
          pills: [],
        };
        const id = groupId(group);
        const existing = actionGroups.get(id) ?? group;
        existing.pills.push({
          id: userRule.id,
          action: definition.id,
          mode: userRule.mode,
          args: argsOf(userRule),
          sequence: effective.sequence,
          userRule,
        });
        actionGroups.set(id, existing);
      }

      if (actionGroups.size === 0) {
        const synthetic: BindingGroup = {
          action: definition.id,
          mode: "global",
          pills: [
            {
              id: `ui.${definition.id}`,
              action: definition.id,
              mode: "global",
              sequence: null,
            },
          ],
        };
        actionGroups.set(groupId(synthetic), synthetic);
      }
      groups.set(definition.id, [...actionGroups.values()]);
    }
    return groups;
  }, [runtimeDefaults, snapshot.defaultRules, snapshot.effectiveRules, snapshot.userRules]);

  const availableModes = useMemo(() => {
    const modes = new Set<KeybindingMode>();
    for (const groups of groupsByAction.values()) {
      for (const group of groups) modes.add(group.mode);
    }
    return KEYBINDING_MODES.filter((mode) => modes.has(mode));
  }, [groupsByAction]);

  useEffect(() => {
    if (modeFilter !== "all" && !availableModes.includes(modeFilter)) setModeFilter("all");
  }, [availableModes, modeFilter]);

  const visibleActions = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return ACTION_CATALOG.flatMap((definition) => {
      const groups = (groupsByAction.get(definition.id) ?? []).filter(
        (group) => modeFilter === "all" || group.mode === modeFilter,
      );
      if (groups.length === 0) return [];
      const searchable = [
        definition.title,
        definition.id,
        ...(definition.keywords ?? []),
        ...groups.flatMap((group) => [
          group.label ?? "",
          ...group.pills.flatMap((pill) =>
            pill.sequence
              ? [serializeSequence(pill.sequence), formatKeybinding(pill.sequence, { isMac })]
              : ["unbound"],
          ),
        ]),
      ]
        .join(" ")
        .toLowerCase();
      return normalized && !searchable.includes(normalized) ? [] : [{ definition, groups }];
    });
  }, [groupsByAction, isMac, modeFilter, query]);

  const restoreFocus = () => {
    const prefix = restoreFocusPrefix.current;
    queueMicrotask(() => {
      if (!prefix) return;
      [...document.querySelectorAll<HTMLButtonElement>("button")]
        .find((button) => button.getAttribute("aria-label")?.startsWith(prefix))
        ?.focus();
    });
  };

  const finishEditing = () => {
    setEditing(null);
    restoreFocus();
  };

  const applyRule = (rule: UserKeybindingRule) => {
    const result = setUserKeybindingCandidate(rule);
    setError(resultMessage(result));
    if (result.ok) {
      setPending(null);
      finishEditing();
    }
  };

  const previewRule = (pill: BindingPill, sequence: string) => {
    if (pill.defaultRule && serializeKeybinding(pill.defaultRule.sequence) === sequence) {
      const result = resetUserKeybindingRule(pill.id);
      setError(resultMessage(result));
      if (result.ok) finishEditing();
      return;
    }
    const current = getKeybindingSnapshot();
    const rule: UserKeybindingRule = {
      id: pill.id,
      action: pill.action,
      mode: pill.mode,
      sequence,
      ...(pill.args === undefined ? {} : { args: pill.args }),
    };
    const prospective = [...current.userRules.filter((item) => item.id !== pill.id), rule];
    const conflicts = resolveEffectiveKeymap(current.defaultRules, prospective).conflicts.filter(
      (conflict) => conflict.first.id === pill.id || conflict.second.id === pill.id,
    );
    const warnings = safetyWarnings(rule);
    if (conflicts.length > 0 || warnings.length > 0) {
      setEditing(null);
      setPending({ rule, conflicts, warnings });
    } else {
      applyRule(rule);
    }
  };

  const removePill = (pill: BindingPill) => {
    const result = pill.defaultRule
      ? unbindDefaultKeybinding(pill.defaultRule)
      : resetUserKeybindingRule(pill.id);
    setError(resultMessage(result));
  };

  const resetWhere = (predicate: (rule: UserKeybindingRule) => boolean) => {
    const current = getKeybindingSnapshot();
    const result = replaceAllUserKeybindings(current.userRules.filter((rule) => !predicate(rule)));
    setError(resultMessage(result));
  };

  return (
    <TooltipProvider>
      <div className="space-y-4" data-testid="keybinding-editor">
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label="Search keyboard shortcuts"
            placeholder="Search actions or shortcuts"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="min-w-52 flex-1"
          />
          <Select
            value={modeFilter}
            onValueChange={(value) => setModeFilter(value as "all" | KeybindingMode)}
          >
            <SelectTrigger data-testid="keybinding-mode-filter" className="w-48">
              <SelectValue placeholder="All modes" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All modes</SelectItem>
              {availableModes.map((mode) => (
                <SelectItem key={mode} value={mode}>
                  {KEYBINDING_MODE_LABELS[mode]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <IconAction
            label="Reset all keyboard shortcuts"
            onClick={() => setConfirmResetAll(true)}
            disabled={snapshot.userRules.length === 0}
          >
            <RotateCcwIcon className="size-4" />
          </IconAction>
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        <div>
          {visibleActions.map(({ definition, groups }) => (
            <div key={definition.id} className="mb-3 rounded-lg border border-border bg-card px-3">
              <div className="flex items-center gap-3 border-b border-border/60 py-2">
                <div className="min-w-0 flex-1 font-medium text-sm">{definition.title}</div>
                <code className="ml-auto break-all text-right text-xs text-muted-foreground">
                  {definition.id}
                </code>
                {snapshot.userRules.some((rule) => rule.action === definition.id) && (
                  <IconAction
                    label="Reset key binding for this action"
                    onClick={() => resetWhere((rule) => rule.action === definition.id)}
                  >
                    <RotateCcwIcon className="size-4" />
                  </IconAction>
                )}
              </div>
              {groups.map((group) => (
                <div
                  key={groupId(group)}
                  className="flex flex-wrap items-center gap-2 border-b border-border/60 py-2 last:border-0"
                >
                  <div className="min-w-40 flex-1 text-xs text-muted-foreground">
                    <span>{group.label ?? KEYBINDING_MODE_LABELS[group.mode]}</span>
                    {group.label && (
                      <span className="ml-1">· {KEYBINDING_MODE_LABELS[group.mode]}</span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center justify-end gap-1.5">
                    {group.pills.length === 0 && (
                      <span className="text-sm text-muted-foreground">Unbound</span>
                    )}
                    {group.pills.map((pill) => {
                      const display = pill.sequence
                        ? formatKeybinding(pill.sequence, { isMac })
                        : "Unbound";
                      const state =
                        pill.sequence === null ? "Unbound" : pill.userRule ? "Modified" : "Default";
                      const label = `${pill.action} ${serializeSequence(pill.sequence)}`;
                      return (
                        <span key={pill.id} className="inline-flex items-center gap-1">
                          <ButtonGroup>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <button
                                  type="button"
                                  data-slot="button"
                                  aria-label={`Rebind ${label}`}
                                  className="inline-flex h-7 cursor-pointer items-center rounded-lg border border-border bg-muted px-2 text-sm font-medium text-muted-foreground hover:border-primary hover:text-foreground"
                                  onClick={() => {
                                    restoreFocusPrefix.current = `Rebind ${pill.action}`;
                                    setEditing(pill);
                                  }}
                                >
                                  {display}
                                </button>
                              </TooltipTrigger>
                              <TooltipContent>Rebind {display}</TooltipContent>
                            </Tooltip>
                            {state !== "Unbound" && (
                              <IconAction
                                label={`Remove ${label}`}
                                onClick={() => removePill(pill)}
                              >
                                <XIcon className="size-4" />
                              </IconAction>
                            )}
                          </ButtonGroup>
                          {pill.sequence && pill.userRule && pill.defaultRule && (
                            <IconAction
                              label={`Reset ${label}`}
                              onClick={() => {
                                const result = resetUserKeybindingRule(pill.id);
                                setError(resultMessage(result));
                              }}
                            >
                              <RotateCcwIcon className="size-4" />
                            </IconAction>
                          )}
                        </span>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          ))}
          {visibleActions.length === 0 && (
            <p className="py-10 text-center text-sm text-muted-foreground">
              No keyboard shortcuts found.
            </p>
          )}
        </div>

        <Dialog open={editing !== null} onOpenChange={(open) => !open && finishEditing()}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Rebind keyboard shortcut</DialogTitle>
              <DialogDescription>
                {editing
                  ? `Recording a shortcut for ${editing.action}.`
                  : "Recording a keyboard shortcut."}
              </DialogDescription>
            </DialogHeader>
            {editing && (
              <KeybindingRecorder
                autoStart
                preferPhysical={editing.defaultRule?.sequence[0].key.kind === "code"}
                onComplete={(sequence) => previewRule(editing, sequence)}
                onCancel={finishEditing}
              />
            )}
          </DialogContent>
        </Dialog>

        <Dialog
          open={pending !== null}
          onOpenChange={(open) => {
            if (!open) {
              setPending(null);
              restoreFocus();
            }
          }}
        >
          <DialogContent className="min-w-0">
            <DialogHeader>
              <DialogTitle>
                {pending?.conflicts.length ? "Shortcut already in use" : "Shortcut may interfere"}
              </DialogTitle>
              <DialogDescription>
                {pending?.conflicts.length
                  ? "Review how this assignment will behave before saving it."
                  : "This assignment may override familiar browser or editing behavior."}
              </DialogDescription>
            </DialogHeader>
            <div className="min-w-0 space-y-3">
              {pending?.warnings.map((warning) => (
                <div
                  key={warning}
                  className="flex gap-2 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm"
                >
                  <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-warning" />
                  <span>{warning}</span>
                </div>
              ))}
              {pending?.conflicts.map((conflict) => (
                <div
                  key={`${conflict.first.id}-${conflict.second.id}`}
                  data-testid="shortcut-conflict-card"
                  className="w-full min-w-0 max-w-full overflow-hidden rounded-lg border border-border"
                >
                  <div className="flex items-center justify-between bg-muted/50 px-3 py-2">
                    <span className="text-xs font-medium text-muted-foreground">Shortcut</span>
                    <KeybindingSequence
                      sequence={
                        pending.rule.sequence ? parseKeybinding(pending.rule.sequence) : null
                      }
                    />
                  </div>
                  <div className="divide-y divide-border">
                    {[conflict.first, conflict.second].map((rule) => {
                      const isNew =
                        rule.id === pending.rule.id && rule.action === pending.rule.action;
                      const isWinner =
                        conflict.resolution === "ambiguous" && conflict.winner.id === rule.id;
                      return (
                        <div
                          key={`${rule.id}-${rule.action}`}
                          className="flex min-w-0 items-start gap-3 p-3"
                        >
                          <div className="min-w-0 flex-1">
                            <div className="text-sm font-medium">{actionTitle(rule.action)}</div>
                            <code className="block break-all text-xs text-muted-foreground">
                              {rule.action}
                            </code>
                          </div>
                          <div className="flex shrink-0 flex-col items-end gap-1">
                            <Badge variant="outline">{isNew ? "New" : "In use"}</Badge>
                            {isWinner && (
                              <span className="text-xs font-medium text-muted-foreground">
                                Runs first
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  <div className="flex gap-2 border-t border-border bg-warning/10 px-3 py-2.5 text-sm">
                    <AlertTriangleIcon className="mt-0.5 size-4 shrink-0 text-warning" />
                    <span>
                      {conflict.resolution === "ambiguous"
                        ? `${actionTitle(conflict.winner.action)} will run when both actions are available.`
                        : "The action for the focused area will run."}
                    </span>
                  </div>
                </div>
              ))}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setPending(null);
                  restoreFocus();
                }}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={() => pending && applyRule(pending.rule)}
              >
                Save anyway
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <Dialog open={confirmResetAll} onOpenChange={setConfirmResetAll}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Reset all keyboard shortcuts?</DialogTitle>
              <DialogDescription>This removes every saved shortcut override.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setConfirmResetAll(false)}>
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                onClick={() => {
                  const result = resetAllUserKeybindings();
                  setError(resultMessage(result));
                  if (result.ok) setConfirmResetAll(false);
                }}
              >
                Reset all
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </TooltipProvider>
  );
}

function serializeSequence(sequence: KeybindingRule["sequence"] | null): string {
  return sequence ? serializeKeybinding(sequence) : "unbound";
}
