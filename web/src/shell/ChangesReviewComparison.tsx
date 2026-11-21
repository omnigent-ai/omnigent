// Changes review's compared revisions: a trigger naming the comparison in the
// toolbar, and a popover with quick choices above the manual Base and Head.
// Where the session's machine can compare named revisions, both are typed, with
// branch and tag suggestions. Elsewhere Base picks one of the comparisons this
// session supports and Head is the working copy; a note says why. The branch's
// own commits are listed in the review's Commits tab, not here.

import { CheckIcon, ChevronDownIcon } from "lucide-react";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  type RevisionField,
  sameRevisions,
  typedRevisions,
  WORKING_COPY_HEAD,
} from "@/lib/customComparison";
import type { LocalComparisonView, QuickComparison } from "@/lib/localComparison";
import { cn } from "@/lib/utils";
import type { RepositoryRefs } from "@/lib/workingCopyComparison";

/** The trigger and its popover; `open` and `onOpenChange` let a recovery open it. */
export function ComparisonPicker({
  view,
  open,
  onOpenChange,
  suggestions,
}: {
  view: LocalComparisonView;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Branch and tag names for typed revisions, once listed. */
  suggestions?: RepositoryRefs;
}) {
  const { trigger } = view;
  const close = () => onOpenChange(false);
  // Opening lands on the marked quick choice, or the first that can run; otherwise Radix's default.
  const focusQuickChoice = (event: Event) => {
    // Radix dispatches the open-focus event on the popover's content.
    const content = event.target instanceof HTMLElement ? event.target : null;
    const choices = content?.querySelectorAll<HTMLElement>("[data-quick-choice]");
    const enabled = [...(choices ?? [])].filter(
      (choice) => choice.getAttribute("aria-disabled") !== "true",
    );
    const target =
      enabled.find((choice) => choice.getAttribute("aria-pressed") === "true") ?? enabled[0];
    if (!target) return;
    event.preventDefault();
    target.focus();
  };
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          aria-label="Change compared revisions"
          title={trigger.title}
          data-invalid={trigger.invalid || undefined}
          data-testid="changes-review-comparison"
          className={cn(
            "h-6 max-w-[min(16rem,50cqw)] min-w-0 shrink gap-1 px-1.5 text-xs text-muted-foreground hover:text-foreground data-[state=open]:bg-accent data-[state=open]:text-foreground",
            trigger.name === null && "font-mono",
            trigger.invalid && "text-destructive hover:text-destructive",
          )}
        >
          {trigger.name !== null ? (
            <span className="truncate">{trigger.name}</span>
          ) : (
            <>
              {trigger.base !== null && (
                <>
                  <span className="truncate">{trigger.base}</span>
                  <span aria-hidden="true" className="text-muted-foreground">
                    …
                  </span>
                </>
              )}
              <span className="truncate">{trigger.head}</span>
            </>
          )}
          <ChevronDownIcon aria-hidden="true" className="size-3 shrink-0 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className={cn(view.revisions.editable ? "w-[min(36rem,calc(100vw-2rem))] p-2" : "w-80 p-3")}
        aria-label="Compared revisions"
        onOpenAutoFocus={focusQuickChoice}
      >
        <QuickChoices choices={view.quickChoices} onChose={close} />
        {view.quickChoices.length > 0 && (
          <div role="separator" aria-hidden="true" className="-mx-1 h-px bg-border" />
        )}
        {view.revisions.editable ? (
          <RevisionFields view={view} suggestions={suggestions} onCompared={close} />
        ) : (
          <PresetFields view={view} />
        )}
      </PopoverContent>
    </Popover>
  );
}

/**
 * The comparisons a person can pick in one step. Each is a button: activating
 * any of them, the marked one too, runs its command once and closes the popover.
 */
function QuickChoices({
  choices,
  onChose,
}: {
  choices: readonly QuickComparison[];
  onChose: () => void;
}) {
  const ids = useId();
  if (choices.length === 0) return null;
  return (
    <div role="group" aria-label="Compare" className="flex flex-col gap-0.5">
      {choices.map((choice) => {
        const unavailable = choice.unavailableReason !== null;
        const labelledBy = `${ids}-${choice.id}-label`;
        const describedBy = `${ids}-${choice.id}-description`;
        return (
          <Button
            key={choice.id}
            type="button"
            variant="ghost"
            data-quick-choice={choice.id}
            aria-pressed={choice.active}
            aria-disabled={unavailable || undefined}
            aria-labelledby={labelledBy}
            aria-describedby={describedBy}
            className={cn(
              "h-auto w-full items-start justify-start gap-2 px-2 py-1.5 text-left font-normal whitespace-normal",
              unavailable && "cursor-default opacity-60 hover:bg-transparent",
            )}
            onClick={() => {
              // A disabled choice stays focusable so its reason can be read, but runs nothing.
              if (unavailable) return;
              choice.choose();
              onChose();
            }}
          >
            <CheckIcon
              aria-hidden="true"
              className={cn("mt-0.5 size-3.5 shrink-0", !choice.active && "invisible")}
            />
            <span className="flex min-w-0 flex-col gap-0.5">
              <span id={labelledBy} className="text-ui text-foreground">
                {choice.label}
              </span>
              <span id={describedBy} className="text-xs text-muted-foreground">
                {choice.unavailableReason ?? choice.description}
              </span>
            </span>
          </Button>
        );
      })}
    </div>
  );
}

/** Where a field's text starts: the typed comparison, or the one on screen. */
function initialText(view: LocalComparisonView): { base: string; head: string } {
  const { custom } = view.revisions;
  if (custom) return { base: custom.base, head: custom.head || WORKING_COPY_HEAD };
  const selected = view.presets.find((preset) => preset.selected);
  const base =
    selected?.id === "head" && view.trigger.base === "HEAD"
      ? "HEAD"
      : selected?.id === "branch-point" && !view.trigger.invalid
        ? (view.trigger.base ?? "")
        : "";
  return { base, head: WORKING_COPY_HEAD };
}

function invalidFor(error: { field: RevisionField } | null, field: "base" | "head"): boolean {
  return error !== null && (error.field === field || error.field === "both");
}

/** Typed Base and Head, as the reference lays them out in one row, with Reset and Compare. */
function RevisionFields({
  view,
  suggestions,
  onCompared,
}: {
  view: LocalComparisonView;
  suggestions?: RepositoryRefs;
  onCompared: () => void;
}) {
  const { revisions, reset, fallback } = view;
  // The fields hold a draft of the applied comparison; a newly applied one replaces it.
  const applied = initialText(view);
  const appliedKey = JSON.stringify(applied);
  const [draft, setDraft] = useState({ appliedKey, text: applied });
  const [typedError, setTypedError] = useState<{ field: RevisionField; message: string } | null>(
    null,
  );
  if (draft.appliedKey !== appliedKey) {
    setDraft({ appliedKey, text: applied });
    setTypedError(null);
  }
  const text = draft.appliedKey === appliedKey ? draft.text : applied;
  const ids = useId();
  const typed = typedRevisions(text.base, text.head);
  const appliedSpec = typedRevisions(applied.base, applied.head);
  const edited = text.base !== applied.base || text.head !== applied.head;
  // Comparing what's already applied changes nothing.
  const unchanged =
    "spec" in typed && "spec" in appliedSpec && sameRevisions(typed.spec, appliedSpec.spec);
  // The app's answer applies only while the fields still say what it answered.
  const answered =
    "spec" in typed && sameRevisions(typed.spec, revisions.custom) ? revisions.fieldError : null;
  const error = typedError ?? answered;
  const names = suggestions?.refs ?? [];
  const suggestionsTitle = suggestions?.truncated
    ? "Suggestions show the first 1,000 branches and tags."
    : undefined;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if ("field" in typed) {
      setTypedError(typed);
      return;
    }
    setTypedError(null);
    revisions.compare(typed.spec);
    onCompared();
  };
  const edit = (field: "base" | "head", value: string) => {
    setTypedError(null);
    setDraft((current) => ({ ...current, text: { ...current.text, [field]: value } }));
  };
  // Reset discards the draft, and goes back to the default comparison if it isn't applied.
  const resetAll = () => {
    setTypedError(null);
    setDraft({ appliedKey, text: applied });
    if (!reset.disabled) reset.run();
  };
  return (
    <form className="flex flex-col gap-1.5 text-ui" onSubmit={submit} noValidate>
      <div className="flex items-center gap-1.5">
        <InputGroup className="h-7 min-w-0 flex-1">
          <InputGroupAddon className="border-r border-input py-0 pr-2 text-[10px] font-semibold tracking-wide uppercase">
            Base
          </InputGroupAddon>
          <InputGroupInput
            aria-label="Base revision"
            aria-invalid={invalidFor(error, "base") || undefined}
            aria-describedby={error ? `${ids}-error` : undefined}
            list={`${ids}-base`}
            title={suggestionsTitle}
            value={text.base}
            placeholder="Branch, tag or commit"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            onChange={(event) => edit("base", event.target.value)}
            className="h-7 font-mono text-xs"
          />
        </InputGroup>
        <span aria-hidden="true" className="text-muted-foreground">
          …
        </span>
        <InputGroup className="h-7 min-w-0 flex-1">
          <InputGroupAddon className="border-r border-input py-0 pr-2 text-[10px] font-semibold tracking-wide uppercase">
            Head
          </InputGroupAddon>
          <InputGroupInput
            aria-label="Head revision"
            aria-invalid={invalidFor(error, "head") || undefined}
            aria-describedby={error ? `${ids}-error` : undefined}
            list={`${ids}-head`}
            title={suggestionsTitle}
            value={text.head}
            placeholder={WORKING_COPY_HEAD}
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            onChange={(event) => edit("head", event.target.value)}
            className="h-7 font-mono text-xs"
          />
        </InputGroup>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={reset.disabled && !edited}
          title="Reset to the default comparison"
          onClick={resetAll}
        >
          Reset
        </Button>
        <Button
          type="submit"
          variant="outline"
          size="xs"
          disabled={text.base.trim() === "" || unchanged}
        >
          Compare
        </Button>
      </div>
      <datalist id={`${ids}-base`}>
        {["HEAD", ...names].map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      <datalist id={`${ids}-head`}>
        {[WORKING_COPY_HEAD, "HEAD", ...names].map((name) => (
          <option key={name} value={name} />
        ))}
      </datalist>
      {error && (
        <p id={`${ids}-error`} className="px-1 text-xs text-destructive" role="alert">
          {error.message}
        </p>
      )}
      {fallback && (
        <p
          className="px-1 text-xs text-muted-foreground"
          data-testid="changes-review-fallback-reason"
        >
          {fallback.message}
        </p>
      )}
    </form>
  );
}

/** Base picks a supported comparison and Head is the working copy, with why nothing can be typed. */
function PresetFields({ view }: { view: LocalComparisonView }) {
  const { trigger, presets, reset, revisionsNote, fallback, quickChoices } = view;
  // A reason a quick choice above already gives isn't repeated below.
  const given = new Set(quickChoices.map((choice) => choice.unavailableReason));
  const note = (text: string | null | undefined) => (text && !given.has(text) ? text : null);
  const selected = presets.find((preset) => preset.selected);
  return (
    <div className="flex flex-col gap-2 text-ui">
      <label className="flex items-center gap-2">
        <span className="w-10 shrink-0 text-xs font-semibold text-muted-foreground uppercase">
          Base
        </span>
        <Select
          value={selected?.id}
          onValueChange={(id) => presets.find((preset) => preset.id === id)?.choose()}
          componentId="changes_review.comparison_base"
          valueHasNoPii
        >
          <SelectTrigger aria-label="Base revision" size="sm" className="min-w-0 flex-1 font-mono">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {presets.map((preset) => (
              <SelectItem
                key={preset.id}
                value={preset.id}
                disabled={preset.unavailableReason !== null}
                title={preset.unavailableReason ?? undefined}
                className="font-mono"
              >
                {preset.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>
      <label className="flex items-center gap-2">
        <span className="w-10 shrink-0 text-xs font-semibold text-muted-foreground uppercase">
          Head
        </span>
        <input
          aria-label="Head revision"
          value={trigger.head}
          readOnly
          className="min-w-0 flex-1 rounded-md border border-input bg-muted/40 px-2 py-1 font-mono text-xs text-muted-foreground"
        />
      </label>
      {trigger.base === null && note(trigger.title) && (
        <p className="text-xs text-muted-foreground">{trigger.title}</p>
      )}
      {trigger.invalid && trigger.base !== null && (
        <p className="text-xs text-destructive" role="alert">
          {trigger.title}
        </p>
      )}
      {fallback && note(fallback.message) && (
        <p className="text-xs text-muted-foreground" data-testid="changes-review-fallback-reason">
          {fallback.message}
        </p>
      )}
      {note(revisionsNote) && (
        <p className="text-xs text-muted-foreground" data-testid="changes-review-revisions-note">
          {revisionsNote}
        </p>
      )}
      <div className="flex justify-end">
        <Button
          type="button"
          variant="ghost"
          size="xs"
          disabled={reset.disabled}
          title="Reset to the default comparison"
          onClick={reset.run}
        >
          Reset
        </Button>
      </div>
    </div>
  );
}
