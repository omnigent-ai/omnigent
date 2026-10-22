// The Summary tab's Labels section: PR label chips and, for editors, a picker over
// the repository's labels. Pending edits show at once and settle to what the provider reports.

import { useMemo, useState } from "react";
import { useMutationState } from "@tanstack/react-query";
import { PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  applyPullRequestLabelChange,
  pullRequestLabelsMutationKey,
  usePullRequestRepoLabels,
  useUpdatePullRequestLabels,
  type PullRequestLabel,
  type PullRequestLabelChange,
} from "@/hooks/usePullRequests";

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

/** A tinted chip, so any label color stays readable in both themes. */
function PullRequestLabelChip({
  label,
  onRemove,
}: {
  label: PullRequestLabel;
  onRemove?: () => void;
}) {
  const color = labelColor(label);
  return (
    <li
      title={label.description ?? undefined}
      className="inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full border border-border bg-muted/20 px-2 py-0.5 text-xs"
      style={color ? { backgroundColor: `${color}1f`, borderColor: `${color}80` } : undefined}
    >
      <LabelDot label={label} />
      <span className="min-w-0 truncate font-medium text-foreground">{label.name}</span>
      {onRemove && (
        <button
          type="button"
          aria-label={`Remove label ${label.name}`}
          onClick={onRemove}
          className="-mr-1 shrink-0 rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <XIcon aria-hidden className="size-3" />
        </button>
      )}
    </li>
  );
}

/** Repository labels with the applied ones checked; choosing one toggles it. */
function PullRequestLabelPicker({
  conversationId,
  prUrl,
  applied,
  onToggle,
}: {
  conversationId: string;
  prUrl: string;
  applied: ReadonlySet<string>;
  onToggle: (label: PullRequestLabel) => void;
}) {
  const [open, setOpen] = useState(false);
  const repoLabels = usePullRequestRepoLabels(conversationId, prUrl, open);
  const labels = repoLabels.data?.data ?? [];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Edit labels"
          // Negative margins keep the header row at text height, level with Reviewers.
          className="-my-1 text-muted-foreground"
        >
          <PlusIcon aria-hidden className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="p-0">
        <Command label="Filter labels">
          <CommandInput placeholder="Filter labels…" />
          <CommandList>
            {repoLabels.isPending ? (
              <p role="status" className="py-6 text-center text-ui text-muted-foreground">
                Loading labels…
              </p>
            ) : repoLabels.isError ? (
              <p role="alert" className="px-2 py-6 text-center text-ui text-destructive">
                {repoLabels.error.message}
              </p>
            ) : (
              <>
                <CommandEmpty>
                  {labels.length === 0 ? "This repository has no labels." : "No labels match."}
                </CommandEmpty>
                <CommandGroup>
                  {labels.map((label) => (
                    <CommandItem
                      key={label.name}
                      value={label.name}
                      keywords={label.description ? [label.description] : undefined}
                      data-checked={applied.has(label.name.toLowerCase())}
                      onSelect={() => onToggle(label)}
                    >
                      <LabelDot label={label} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">{label.name}</span>
                        {label.description && (
                          <span className="block truncate text-xs text-muted-foreground">
                            {label.description}
                          </span>
                        )}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
          {repoLabels.data?.has_more && (
            <p className="border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
              Showing the first {labels.length} labels.
            </p>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  );
}

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
  const update = useUpdatePullRequestLabels(conversationId);
  const pending = useMutationState({
    filters: { mutationKey: pullRequestLabelsMutationKey(conversationId), status: "pending" },
    select: (mutation) => mutation.state.variables as PullRequestLabelChange | undefined,
  });
  const shown = useMemo(
    () =>
      [
        ...pending.reduce(
          (current, change) =>
            change?.prUrl === prUrl ? applyPullRequestLabelChange(current, change) : current,
          labels,
        ),
      ].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" })),
    [labels, pending, prUrl],
  );
  const applied = useMemo(() => new Set(shown.map((label) => label.name.toLowerCase())), [shown]);
  const toggle = (label: PullRequestLabel) => {
    const remove = applied.has(label.name.toLowerCase());
    update.mutate({ prUrl, add: remove ? [] : [label], remove: remove ? [label] : [] });
  };

  return (
    <section className="space-y-1.5" aria-labelledby="pull-request-labels-heading">
      <div className="flex items-center gap-1">
        <h3
          id="pull-request-labels-heading"
          className="text-xs font-medium tracking-wide text-muted-foreground uppercase"
        >
          Labels
        </h3>
        {editable && (
          <PullRequestLabelPicker
            conversationId={conversationId}
            prUrl={prUrl}
            applied={applied}
            onToggle={toggle}
          />
        )}
      </div>
      {shown.length === 0 ? (
        <p className="text-ui text-muted-foreground">No labels.</p>
      ) : (
        <ul aria-label="Labels" className="flex flex-wrap gap-1.5">
          {shown.map((label) => (
            <PullRequestLabelChip
              key={label.name}
              label={label}
              onRemove={editable ? () => toggle(label) : undefined}
            />
          ))}
        </ul>
      )}
      {update.isError && update.variables?.prUrl === prUrl && (
        <p role="alert" className="text-ui text-destructive">
          {update.error.message}
        </p>
      )}
    </section>
  );
}
