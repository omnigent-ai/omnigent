// The Summary tab's Labels section: PR label chips and, for editors, a picker over
// the repository's labels. An edit shows as in progress until the provider answers.

import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { useWorkspaceDraftOwner } from "@/lib/workspaceDraftOwner";
import { useChangeStatus } from "@/components/pendingChange/changeStatus";
import {
  ChangeFailedNotice,
  pendingChangeClassName,
  PendingChangeIcon,
} from "@/components/pendingChange/PendingChange";
import { useCallback, useEffect, useMemo, useState } from "react";
import { usePickerDraft } from "@/hooks/usePickerDraft";
import { pickerDraftSummary, pickerDraftTargets } from "@/lib/pickerDraft";
import { cn } from "@/lib/utils";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { labelPickerGroups } from "@/lib/labelPickerList";
import { reconcileRecentLabels, useRecentLabels } from "@/lib/recentLabels";
import { Loader2Icon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandList,
} from "@/components/ui/command";
import { useTakenControlRequest } from "@/pullRequestPage/pullRequestDestination";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  pullRequestLabelsMutationKey,
  usePullRequestLabelSearch,
  usePullRequestLabelSearchOffered,
  usePullRequestRepoLabels,
  type PullRequestLabel,
  type PullRequestLabelChange,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  LABEL_CHANGE_WAITING_REASON,
  labelFailurePrefix,
  labelChangeIsCurrent,
  labelKey,
  type LabelEdit,
  usePullRequestLabelChanges,
} from "../../shell/pullRequestLabelChanges";
import {
  PULL_REQUEST_CHIP_CLASS,
  PullRequestChipAction,
  PullRequestSectionHeader,
} from "../../shell/PullRequestSection";
import { SECTION_PICKER_TRIGGER_CLASS, useSectionPicker } from "@/shell/pullRequestSectionStyle";
import { CheckboxCommandItem } from "@/pullRequestPage/CheckboxCommandItem";

/** The label's `#rrggbb` color, or undefined when the provider sent none. */
function labelColor(label: PullRequestLabel): string | undefined {
  return label.color && /^[0-9a-f]{6}$/i.test(label.color) ? `#${label.color}` : undefined;
}

function LabelDot({ label }: { label: PullRequestLabel }) {
  const color = labelColor(label);
  return (
    <span
      aria-hidden
      className="size-2 shrink-0 rounded-full bg-muted-foreground"
      style={color ? { backgroundColor: color } : undefined}
    />
  );
}

const LABEL_EDIT_LABEL: Record<LabelEdit, string> = {
  adding: "Adding…",
  removing: "Removing…",
};

const LABEL_DRAFT_WORDS = {
  add: "to add",
  remove: "to remove",
  sends: "applied when you close",
};

/** A tinted chip, so any label color stays readable in both themes. */
function PullRequestLabelChip({
  label,
  edit,
  onRemove,
}: {
  label: PullRequestLabel;
  edit?: LabelEdit;
  onRemove?: () => void;
}) {
  const color = labelColor(label);
  return (
    <li
      title={edit ? LABEL_EDIT_LABEL[edit] : (label.description ?? undefined)}
      aria-label={edit ? `${label.name}: ${LABEL_EDIT_LABEL[edit]}` : undefined}
      aria-busy={edit ? true : undefined}
      className={cn(
        PULL_REQUEST_CHIP_CLASS,
        "rounded-full",
        edit && pendingChangeClassName(edit === "removing" ? "removing" : "changing"),
      )}
      style={color ? { backgroundColor: `${color}1f`, borderColor: `${color}80` } : undefined}
    >
      <LabelDot label={label} />
      <span className="min-w-0 truncate font-medium text-foreground">{label.name}</span>
      {edit && <PendingChangeIcon className="size-3" />}
      {onRemove && !edit && (
        <PullRequestChipAction
          label={`Remove label ${label.name}`}
          icon={XIcon}
          onClick={onRemove}
        />
      )}
    </li>
  );
}

/** One label row: choosing it checks or unchecks it, applied when the picker closes. */
function LabelOption({
  label,
  checked,
  edit,
  onToggle,
}: {
  label: PullRequestLabel;
  checked: boolean;
  edit: LabelEdit | undefined;
  onToggle: (label: PullRequestLabel) => void;
}) {
  return (
    <CheckboxCommandItem
      checked={checked}
      value={label.name}
      keywords={label.description ? [label.description] : undefined}
      onSelect={() => onToggle(label)}
    >
      <LabelDot label={label} />
      <span className="min-w-0 flex-1">
        <span className="block truncate">{label.name}</span>
        {label.description && (
          <span className="block truncate text-xs text-muted-foreground">{label.description}</span>
        )}
      </span>
      {edit && (
        <Loader2Icon
          role="img"
          aria-label={LABEL_EDIT_LABEL[edit]}
          className="size-3.5 animate-spin text-muted-foreground"
        />
      )}
    </CheckboxCommandItem>
  );
}

const PICKER_NOTE_CLASS = "border-t border-border px-3 py-1.5 text-xs";

/**
 * Repository labels with a checkbox each, checked where `checked` says; choosing one toggles it,
 * and closing applies them all as one change. The labels this user added recently come first. Where the repository has more labels than one read lists, typed text
 * also searches GitHub, so labels past the list can be found.
 */
function PullRequestLabelPicker({
  conversationId,
  prUrl,
  checked,
  chosen,
  summary,
  edits,
  onToggle,
  waiting,
  picker,
}: {
  conversationId: string;
  prUrl: string;
  checked: ReadonlySet<string>;
  /** The labels checked while open, listed even when nothing else lists them. */
  chosen: readonly PullRequestLabel[];
  /** What closing will apply, such as "1 to add · applied when you close". */
  summary: string | null;
  edits: ReadonlyMap<string, LabelEdit>;
  onToggle: (label: PullRequestLabel) => void;
  /** A change is still being made on this Mac; the picker opens again once it lands. */
  waiting: boolean;
  picker: ReturnType<typeof useSectionPicker>;
}) {
  const { open, setOpen } = picker;
  const [query, setQuery] = useState("");
  const repoLabels = usePullRequestRepoLabels(conversationId, prUrl, open);
  const labels = repoLabels.data?.data ?? NO_LABELS;
  const hasMore = repoLabels.data?.has_more ?? false;
  const scope = useMemo(() => {
    const target = parseCodeHostPullRequestUrl(prUrl);
    return target ? { host: target.host, repository: target.repository } : null;
  }, [prUrl]);
  const recent = useRecentLabels(scope);
  const searchOffered = usePullRequestLabelSearchOffered(conversationId, prUrl);
  const search = usePullRequestLabelSearch(conversationId, prUrl, query, open && hasMore);
  // A whole list from GitHub settles the remembered labels.
  const complete = repoLabels.data && !hasMore && !repoLabels.data.restored ? labels : null;
  useEffect(() => {
    if (scope && complete) reconcileRecentLabels(scope, { kind: "complete", labels: complete });
  }, [scope, complete]);
  const typed = query.trim();
  const matches = typed && search.data ? search.data.data : NO_LABELS;
  const groups = labelPickerGroups({ listed: labels, recent, matches, chosen });
  const searching = hasMore && searchOffered && typed.length > 0;
  const option = (label: PullRequestLabel) => (
    <LabelOption
      key={label.name}
      label={label}
      checked={checked.has(labelKey(label))}
      edit={edits.get(labelKey(label))}
      onToggle={onToggle}
    />
  );
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) setQuery("");
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Edit labels"
          className={SECTION_PICKER_TRIGGER_CLASS}
          disabled={waiting}
          title={waiting ? LABEL_CHANGE_WAITING_REASON : undefined}
        >
          <PlusIcon aria-hidden className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="p-0"
        onPointerDownOutside={picker.onPointerDownOutside}
      >
        <Command label="Filter labels">
          <CommandInput placeholder="Filter labels…" value={query} onValueChange={setQuery} />
          <CommandList>
            {repoLabels.isPending ? (
              <p role="status" className="py-6 text-center text-ui text-muted-foreground">
                Loading labels…
              </p>
            ) : repoLabels.isError && !repoLabels.data ? (
              <p role="alert" className="px-2 py-6 text-center text-ui text-destructive">
                {repoLabels.error.message}
              </p>
            ) : (
              <>
                <CommandEmpty>
                  {labels.length === 0 && recent.length === 0 && !searching
                    ? "This repository has no labels."
                    : searching
                      ? "No listed labels match."
                      : "No labels match."}
                </CommandEmpty>
                {groups.recent.length > 0 && (
                  <CommandGroup heading="Recent">{groups.recent.map(option)}</CommandGroup>
                )}
                {groups.listed.length > 0 && (
                  <CommandGroup heading={groups.recent.length > 0 ? "All labels" : undefined}>
                    {groups.listed.map(option)}
                  </CommandGroup>
                )}
                {groups.found.length > 0 && (
                  <CommandGroup heading="More on GitHub">{groups.found.map(option)}</CommandGroup>
                )}
              </>
            )}
          </CommandList>
          {searching && search.isError && (
            <p role="alert" className={cn(PICKER_NOTE_CLASS, "text-destructive")}>
              Couldn’t search GitHub: {search.error.message}
            </p>
          )}
          {searching && !search.isError && (search.isFetching || (!search.isFetched && typed)) && (
            <p role="status" className={cn(PICKER_NOTE_CLASS, "text-muted-foreground")}>
              Searching GitHub for more labels…
            </p>
          )}
          {searching && search.isSuccess && !search.isFetching && groups.found.length === 0 && (
            <p role="status" className={cn(PICKER_NOTE_CLASS, "text-muted-foreground")}>
              No other labels match on GitHub.
            </p>
          )}
          {repoLabels.isError && repoLabels.data && (
            // The labels from before stay usable; only the refresh behind them failed.
            <p role="alert" className={cn(PICKER_NOTE_CLASS, "text-destructive")}>
              Couldn’t refresh labels: {repoLabels.error.message}
            </p>
          )}
          {summary && (
            <p aria-live="polite" className={cn(PICKER_NOTE_CLASS, "text-muted-foreground")}>
              {summary}
            </p>
          )}
          {hasMore && !searchOffered && (
            <p className={cn(PICKER_NOTE_CLASS, "text-muted-foreground")}>
              Showing the first {labels.length} labels.
            </p>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  );
}

const NO_LABELS: PullRequestLabel[] = [];

export function PullRequestLabels({
  conversationId,
  prUrl,
  labels,
  editable,
}: {
  conversationId: string;
  prUrl: string;
  labels: PullRequestLabel[];
  editable: boolean;
}) {
  const { changes, applied, waiting, send } = usePullRequestLabelChanges(
    conversationId,
    prUrl,
    labels,
  );
  // Each label's latest unanswered edit; its chip stays, struck or muted, until GitHub answers.
  const edits = useMemo(() => {
    const latest = new Map<string, LabelEdit>();
    for (const change of changes) {
      for (const label of change.add) latest.set(labelKey(label), "adding");
      for (const label of change.remove) latest.set(labelKey(label), "removing");
    }
    return latest;
  }, [changes]);
  const shown = useMemo(() => {
    const byKey = new Map(labels.map((label) => [labelKey(label), label]));
    for (const change of changes) {
      for (const label of change.add)
        if (!byKey.has(labelKey(label))) byKey.set(labelKey(label), label);
    }
    return [...byKey.values()].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    );
  }, [changes, labels]);
  const picker = useSectionPicker(editable && !waiting);
  // A row's Edit Labels… aimed at this view opens the picker once it can edit.
  useTakenControlRequest(
    prUrl,
    "labels",
    editable && !waiting,
    () => picker.setOpen(true),
    picker.open,
  );
  const toggle = (label: PullRequestLabel) => {
    if (waiting) return;
    const remove = applied.has(labelKey(label));
    send({ prUrl, add: remove ? [] : [label], remove: remove ? [label] : [] });
  };
  // The picker's checks while it's open, applied as one change once it closes and nothing else
  // is still being sent from this Mac, and only as whoever made them.
  const draft = usePickerDraft({
    prUrl: editable ? prUrl : null,
    current: applied,
    open: picker.open,
    waiting,
    keyOf: labelKey,
    send,
  });
  // The latest label change to this PR, as the account in use now made it; re-read when the
  // workspace user changes, which no mutation event announces.
  const owner = useWorkspaceDraftOwner();
  const ownChange = useCallback(
    (change: PullRequestLabelChange) =>
      change.prUrl === prUrl && labelChangeIsCurrent(change, { owner }),
    [prUrl, owner],
  );
  const status = useChangeStatus(pullRequestLabelsMutationKey(conversationId), ownChange);
  // A refusal may be tried again; a change GitHub may have made waits for the next read.
  const retry =
    status.kind === "failed" &&
    editable &&
    !waiting &&
    !(status.error instanceof PullRequestWriteError && status.error.outcomeUnknown)
      ? () =>
          send({
            prUrl: status.variables.prUrl,
            add: status.variables.add,
            remove: status.variables.remove,
          })
      : undefined;

  return (
    <section
      {...picker.sectionProps}
      className={cn("space-y-1.5", picker.sectionProps.className)}
      aria-labelledby="pull-request-labels-heading"
    >
      <PullRequestSectionHeader
        id="pull-request-labels-heading"
        title="Labels"
        action={
          editable && (
            <PullRequestLabelPicker
              conversationId={conversationId}
              prUrl={prUrl}
              checked={draft.checked}
              chosen={pickerDraftTargets(draft.draft)}
              summary={pickerDraftSummary(draft.change, LABEL_DRAFT_WORDS)}
              edits={edits}
              onToggle={draft.toggle}
              waiting={waiting}
              picker={picker}
            />
          )
        }
      />
      {shown.length === 0 ? (
        <p className="text-ui text-muted-foreground">No labels.</p>
      ) : (
        <ul aria-label="Labels" className="flex flex-wrap gap-1.5">
          {shown.map((label) => (
            <PullRequestLabelChip
              key={label.name}
              label={label}
              edit={edits.get(labelKey(label))}
              onRemove={editable && !waiting ? () => toggle(label) : undefined}
            />
          ))}
        </ul>
      )}
      {status.kind === "failed" && (
        <ChangeFailedNotice
          message={`${labelFailurePrefix(status.variables)}: ${status.error.message}`}
          onRetry={retry}
          onDismiss={status.dismiss}
        />
      )}
    </section>
  );
}
