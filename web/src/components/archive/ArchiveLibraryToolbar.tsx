import {
  CalendarRangeIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronsUpDownIcon,
  RotateCcwIcon,
  SearchIcon,
} from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ArchiveDateRangePicker } from "@/components/archive/ArchiveDateRangePicker";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type {
  ArchivedConversationFilters,
  ArchivedDateField,
  ArchivedSearchScope,
  ArchivedSortField,
} from "@/hooks/useConversations";
import { archiveDateRangeBounds } from "@/lib/archiveDateRange";
import { cn } from "@/lib/utils";

export interface ArchiveLibraryViewState {
  searchQuery: string;
  searchScope: ArchivedSearchScope;
  project?: string;
  hostId?: string;
  agentName?: string;
  dateField: ArchivedDateField;
  dateRange: string;
  sortField: ArchivedSortField;
  order: "asc" | "desc";
}

export interface ArchiveFilterOption {
  value: string;
  label: string;
  keywords?: string;
}

interface ArchiveLibraryToolbarProps {
  value: ArchiveLibraryViewState;
  projectOptions: ArchiveFilterOption[];
  hostOptions: ArchiveFilterOption[];
  agentOptions: ArchiveFilterOption[];
  onChange: (patch: Partial<ArchiveLibraryViewState>) => void;
  className?: string;
}

export function parseArchiveDateRange(value: string): { after: number; before: number } | null {
  return archiveDateRangeBounds(value);
}

export function buildArchiveConversationFilters(
  value: ArchiveLibraryViewState,
  searchQuery: string,
): ArchivedConversationFilters {
  return {
    searchQuery,
    searchScope: value.searchScope,
    project: value.project,
    hostId: value.hostId,
    agentName: value.agentName,
    dateField: value.dateField,
    dateRange: value.dateRange,
    sortField: value.sortField,
    agePreset: "any",
    order: value.order,
  };
}

function FilterCombobox({
  label,
  allLabel,
  value,
  options,
  onChange,
}: {
  label: string;
  allLabel: string;
  value?: string;
  options: ArchiveFilterOption[];
  onChange: (value: string | undefined) => void;
}) {
  const [open, setOpen] = useState(false);
  const current = options.find((option) => option.value === value);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="min-w-0 flex-1">
            <PopoverTrigger asChild>
              <Button
                type="button"
                variant="outline"
                role="combobox"
                aria-label={`Filter archived sessions by ${label.toLowerCase()}`}
                aria-expanded={open}
                className={cn(
                  "h-8 w-full min-w-0 justify-between gap-1 px-2 text-xs font-normal",
                  value && "border-primary/40 bg-primary/5",
                )}
              >
                <span className="truncate">{current?.label ?? value ?? allLabel}</span>
                <ChevronsUpDownIcon className="size-3 shrink-0 text-muted-foreground" />
              </Button>
            </PopoverTrigger>
          </span>
        </TooltipTrigger>
        <TooltipContent>
          {label}: {current?.label ?? allLabel}. Open the list or type to fuzzy search; use ↑/↓ and
          Enter to confirm.
        </TooltipContent>
      </Tooltip>
      <PopoverContent align="start" className="w-(--radix-popover-trigger-width) min-w-48 p-0">
        <Command>
          <CommandInput placeholder={`Search ${label.toLowerCase()}…`} />
          <CommandList>
            <CommandEmpty>No matching option.</CommandEmpty>
            <CommandItem
              value={allLabel}
              onSelect={() => {
                onChange(undefined);
                setOpen(false);
              }}
            >
              <CheckIcon
                className={cn("size-3.5", value === undefined ? "opacity-100" : "opacity-0")}
              />
              {allLabel}
            </CommandItem>
            {options.map((option) => (
              <CommandItem
                key={option.value}
                value={`${option.label} ${option.keywords ?? ""}`}
                onSelect={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
              >
                <CheckIcon
                  className={cn("size-3.5", option.value === value ? "opacity-100" : "opacity-0")}
                />
                <span className="truncate">{option.label}</span>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

function sortLabel(value: ArchiveLibraryViewState): string {
  if (value.sortField === "title") return value.order === "asc" ? "Name A–Z" : "Name Z–A";
  return `${value.sortField === "created_at" ? "Created" : "Archived"} ${value.order === "desc" ? "↓" : "↑"}`;
}

export function ArchiveLibraryToolbar({
  value,
  projectOptions,
  hostOptions,
  agentOptions,
  onChange,
  className,
}: ArchiveLibraryToolbarProps) {
  const hasFilters = Boolean(
    value.searchQuery || value.project || value.hostId || value.agentName || value.dateRange,
  );

  return (
    <div className={cn("space-y-1.5 border-b p-2", className)}>
      <div className="flex min-w-0 items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={value.searchQuery}
            onChange={(event) => onChange({ searchQuery: event.target.value })}
            aria-label={
              value.searchScope === "title"
                ? "Search archived session titles"
                : "Search archived conversation content"
            }
            placeholder={
              value.searchScope === "title" ? "Search session titles…" : "Search messages…"
            }
            className="h-8 pl-8 text-xs"
          />
        </div>
        <div className="flex shrink-0 rounded-md border bg-muted/20 p-0.5">
          {(["title", "content"] as const).map((scope) => (
            <Tooltip key={scope}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  aria-pressed={value.searchScope === scope}
                  className={cn(
                    "h-6 rounded px-1.5 text-[10px] text-muted-foreground",
                    value.searchScope === scope && "bg-background text-foreground shadow-sm",
                  )}
                  onClick={() => onChange({ searchScope: scope, searchQuery: "" })}
                >
                  {scope === "title" ? "Title" : "Content"}
                </button>
              </TooltipTrigger>
              <TooltipContent>
                {scope === "title"
                  ? "Search only session titles."
                  : "Search message and response content, then open the exact matching window."}
              </TooltipContent>
            </Tooltip>
          ))}
        </div>
        <Popover>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0">
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 gap-1 px-2 text-[10px] font-normal"
                    aria-label={`Archive sort: ${sortLabel(value)}`}
                  >
                    {sortLabel(value)} <ChevronDownIcon className="size-3" />
                  </Button>
                </PopoverTrigger>
              </span>
            </TooltipTrigger>
            <TooltipContent>Sort by archive date, creation date, or session name.</TooltipContent>
          </Tooltip>
          <PopoverContent align="end" className="w-52 p-1">
            {(
              [
                ["archived_at", "Archive date"],
                ["created_at", "Create date"],
                ["title", "Name"],
              ] as const
            ).map(([field, label]) => (
              <Button
                key={field}
                type="button"
                variant="ghost"
                size="sm"
                className="w-full justify-start"
                onClick={() =>
                  onChange({ sortField: field, order: field === "title" ? "asc" : "desc" })
                }
              >
                <CheckIcon
                  className={cn(
                    "size-3.5",
                    value.sortField === field ? "opacity-100" : "opacity-0",
                  )}
                />
                {label}
              </Button>
            ))}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="w-full justify-start"
              onClick={() => onChange({ order: value.order === "asc" ? "desc" : "asc" })}
            >
              {value.sortField === "title"
                ? value.order === "asc"
                  ? "Switch to Z–A"
                  : "Switch to A–Z"
                : value.order === "desc"
                  ? "Switch to oldest first"
                  : "Switch to newest first"}
            </Button>
          </PopoverContent>
        </Popover>
      </div>

      <div className="flex min-w-0 items-center gap-1.5">
        <FilterCombobox
          label="Project"
          allLabel="All projects"
          value={value.project}
          options={projectOptions}
          onChange={(project) => onChange({ project })}
        />
        <FilterCombobox
          label="Host"
          allLabel="All hosts"
          value={value.hostId}
          options={hostOptions}
          onChange={(hostId) => onChange({ hostId })}
        />
        <FilterCombobox
          label="Agent"
          allLabel="All agents"
          value={value.agentName}
          options={agentOptions}
          onChange={(agentName) => onChange({ agentName })}
        />
        <Popover>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="shrink-0">
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon-sm"
                    className={cn("size-8", value.dateRange && "border-primary/40 bg-primary/5")}
                    aria-label="Filter archive by date"
                  >
                    <CalendarRangeIcon className="size-3.5" />
                  </Button>
                </PopoverTrigger>
              </span>
            </TooltipTrigger>
            <TooltipContent>Filter by Created, Active, or Archived date.</TooltipContent>
          </Tooltip>
          <PopoverContent align="end" className="w-[20rem] space-y-3 p-3">
            <div
              role="group"
              aria-label="Archive date dimension"
              className="grid grid-cols-3 rounded-md border bg-muted/20 p-0.5"
            >
              {(
                [
                  ["created_at", "Created"],
                  ["active_at", "Active"],
                  ["archived_at", "Archived"],
                ] as const
              ).map(([field, label]) => (
                <button
                  key={field}
                  type="button"
                  aria-pressed={value.dateField === field}
                  className={cn(
                    "h-7 rounded px-2 text-xs text-muted-foreground",
                    value.dateField === field && "bg-background text-foreground shadow-sm",
                  )}
                  onClick={() => onChange({ dateField: field })}
                >
                  {label}
                </button>
              ))}
            </div>
            <ArchiveDateRangePicker
              value={value.dateRange}
              onValueChange={(dateRange) => onChange({ dateRange })}
              inlineCalendar
            />
          </PopoverContent>
        </Popover>
        {hasFilters && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="size-8 shrink-0 text-destructive"
                aria-label="Clear all archive filters"
                onClick={() =>
                  onChange({
                    searchQuery: "",
                    project: undefined,
                    hostId: undefined,
                    agentName: undefined,
                    dateRange: "",
                  })
                }
              >
                <RotateCcwIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Clear archive search and filters.</TooltipContent>
          </Tooltip>
        )}
      </div>
    </div>
  );
}
