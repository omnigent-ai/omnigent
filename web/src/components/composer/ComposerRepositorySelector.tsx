import { useState } from "react";
import {
  AlertCircleIcon,
  ChevronsUpDownIcon,
  FolderGit2Icon,
  GitBranchIcon,
  XIcon,
} from "lucide-react";

import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import type {
  ComposerContextResourceState,
  ComposerRepositorySelection,
} from "@/lib/composerContext";
import { cn } from "@/lib/utils";

const EMPTY_REPOSITORIES: readonly ComposerRepositorySelection[] = [];

export interface ComposerRepositorySelectorProps {
  value: readonly ComposerRepositorySelection[];
  repositories: ComposerContextResourceState<readonly ComposerRepositorySelection[]>;
  onChange: (repositories: ComposerRepositorySelection[]) => void;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
}

function repositoryName(url: string): string {
  const trimmed = url.replace(/\/+$/, "").replace(/\.git$/, "");
  return trimmed.split(/[/:]/).filter(Boolean).at(-1) ?? url;
}

function RepositoryIdentity({ repository }: { repository: ComposerRepositorySelection }) {
  const name = repositoryName(repository.url);
  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      <FolderGit2Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium" title={name}>
          {name}
        </span>
        <span className="block truncate text-xs text-muted-foreground" title={repository.url}>
          {repository.url}
        </span>
      </span>
      {repository.branch && (
        <span
          className="flex max-w-[45%] shrink-0 items-center gap-1 text-xs text-muted-foreground"
          title={repository.branch}
        >
          <GitBranchIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{repository.branch}</span>
        </span>
      )}
    </span>
  );
}

function RepositoryChip({
  repository,
  disabled,
  onRemove,
}: {
  repository: ComposerRepositorySelection;
  disabled: boolean;
  onRemove: () => void;
}) {
  const name = repositoryName(repository.url);
  return (
    <span className="flex h-6 max-w-full min-w-0 items-center gap-1 rounded-md bg-muted px-1.5 text-sm text-foreground dark:bg-muted/60">
      <FolderGit2Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 truncate" title={repository.url}>
        {name}
      </span>
      {repository.branch && (
        <span className="min-w-0 truncate text-muted-foreground" title={repository.branch}>
          · {repository.branch}
        </span>
      )}
      <button
        type="button"
        className="-mr-0.5 flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground outline-none hover:bg-foreground/10 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"
        aria-label={`Remove ${name}${repository.branch ? ` on ${repository.branch}` : ""}`}
        disabled={disabled}
        onClick={onRemove}
      >
        <XIcon className="size-3" aria-hidden="true" />
      </button>
    </span>
  );
}

function ResourceMessage({
  repositories,
}: {
  repositories: ComposerContextResourceState<readonly ComposerRepositorySelection[]>;
}) {
  if (repositories.status === "loading") {
    return (
      <div className="flex items-center justify-center gap-2 px-3 py-6 text-sm text-muted-foreground">
        <Spinner className="size-3.5" />
        Loading repositories…
      </div>
    );
  }
  if (repositories.status === "unavailable") {
    return (
      <div className="px-3 py-6 text-center text-sm text-muted-foreground">
        Repository selection is unavailable.
      </div>
    );
  }
  if (repositories.status === "idle") {
    return (
      <div className="px-3 py-6 text-center text-sm text-muted-foreground">
        Repositories have not loaded yet.
      </div>
    );
  }
  if (repositories.status === "error") {
    return (
      <div
        role="alert"
        className="mx-1 my-1 flex items-start gap-2 rounded-lg bg-destructive/10 px-2.5 py-2 text-sm text-destructive"
      >
        <AlertCircleIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span className="min-w-0 break-words">{repositories.error.message}</span>
      </div>
    );
  }
  if (repositories.status === "stale") {
    return (
      <div className="px-2 py-1 text-xs text-muted-foreground">
        Repository list may be out of date.
      </div>
    );
  }
  return null;
}

export function ComposerRepositorySelector({
  value,
  repositories,
  onChange,
  disabled = false,
  className,
  ariaLabel = "Repositories",
}: ComposerRepositorySelectorProps) {
  const [open, setOpen] = useState(false);

  const selectedIds = new Set(value.map(({ id }) => id));
  const available = repositories.data ?? EMPTY_REPOSITORIES;
  const availableIds = new Set(available.map(({ id }) => id));
  const options = [...available, ...value.filter(({ id }) => !availableIds.has(id))];
  const hasSelectableData = repositories.data !== null || value.length > 0;

  function toggle(repository: ComposerRepositorySelection) {
    if (selectedIds.has(repository.id)) {
      onChange(value.filter(({ id }) => id !== repository.id).map((item) => ({ ...item })));
      return;
    }
    onChange([...value.map((item) => ({ ...item })), { ...repository }]);
  }

  function clearRepositories() {
    onChange([]);
  }

  const selectionLabel =
    value.length === 0
      ? `${ariaLabel}, no repositories selected`
      : `${ariaLabel}, ${value.length} ${value.length === 1 ? "repository" : "repositories"} selected`;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <div
        className={cn(
          "flex min-h-9 w-full min-w-0 items-center gap-1.5 rounded-lg border border-input bg-transparent p-1.5 transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input/30",
          disabled && "pointer-events-none opacity-50",
          className,
        )}
        data-testid="composer-repository-selector"
      >
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
          {value.length === 0 ? (
            <span className="min-w-0 truncate px-1 text-ui text-muted-foreground">
              No repositories
            </span>
          ) : (
            value.map((repository) => (
              <RepositoryChip
                key={repository.id}
                repository={repository}
                disabled={disabled}
                onRemove={() => toggle(repository)}
              />
            ))
          )}
        </div>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 dark:hover:bg-muted/50"
            aria-label={selectionLabel}
            disabled={disabled}
          >
            <ChevronsUpDownIcon className="size-4" aria-hidden="true" />
          </button>
        </PopoverTrigger>
      </div>
      <PopoverContent
        align="start"
        sideOffset={6}
        className="w-(--radix-popover-trigger-width) min-w-64 max-w-[calc(100vw-2rem)] gap-0 p-0"
      >
        <Command className="rounded-lg p-1" label="Search repositories">
          <CommandInput placeholder="Search repositories…" />
          <CommandList aria-label="Available repositories">
            <CommandItem
              value="no repositories intentional none"
              data-checked={value.length === 0 || undefined}
              onSelect={clearRepositories}
              className="min-h-10 rounded-lg px-2"
            >
              <span className="size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="block font-medium">No repositories</span>
                <span className="block truncate text-xs text-muted-foreground">
                  Start without repository context
                </span>
              </span>
            </CommandItem>
            {hasSelectableData &&
              options.map((repository) => {
                const selected = selectedIds.has(repository.id);
                const unavailable = !availableIds.has(repository.id);
                return (
                  <CommandItem
                    key={repository.id}
                    value={`${repositoryName(repository.url)} ${repository.url} ${repository.branch ?? ""}`}
                    data-checked={selected || undefined}
                    aria-selected={selected}
                    onSelect={() => toggle(repository)}
                    className="min-h-11 rounded-lg px-2"
                  >
                    <RepositoryIdentity repository={repository} />
                    {unavailable && (
                      <span className="shrink-0 text-xs text-muted-foreground">Unavailable</span>
                    )}
                  </CommandItem>
                );
              })}
            {hasSelectableData && options.length === 0 && (
              <CommandEmpty>No repositories found.</CommandEmpty>
            )}
            <ResourceMessage repositories={repositories} />
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
