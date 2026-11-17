// Status, project and search filters for the Tile view. "/" focuses search.

import { useEffect, useRef } from "react";
import { SearchIcon, StarIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { TileStatusFilter } from "./tileFilters";

const STATUS_LABELS: Record<TileStatusFilter, string> = {
  all: "All",
  favorites: "Favorites",
  "needs-you": "Needs you",
  working: "Working",
};

interface TileFilterBarProps {
  status: TileStatusFilter;
  counts: Record<TileStatusFilter, number>;
  onStatusChange: (status: TileStatusFilter) => void;
  projects: readonly string[];
  project: string | null;
  onProjectChange: (project: string | null) => void;
  query: string;
  onQueryChange: (query: string) => void;
}

function isTypingTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
  );
}

export function TileFilterBar({
  status,
  counts,
  onStatusChange,
  projects,
  project,
  onProjectChange,
  query,
  onQueryChange,
}: TileFilterBarProps) {
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || isTypingTarget(event.target)) {
        return;
      }
      event.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-3 border-b px-4 py-2">
      <div role="radiogroup" aria-label="Status" className="flex items-center gap-0.5">
        {(Object.keys(STATUS_LABELS) as TileStatusFilter[]).map((option) => (
          <Button
            key={option}
            role="radio"
            aria-checked={status === option}
            variant="ghost"
            size="xs"
            className={cn("gap-1", status === option && "bg-accent text-accent-foreground")}
            onClick={() => onStatusChange(option)}
          >
            {option === "favorites" && <StarIcon className="size-3.5" aria-hidden />}
            {STATUS_LABELS[option]}
            <span className="text-muted-foreground tabular-nums">{counts[option]}</span>
          </Button>
        ))}
      </div>

      {projects.length > 1 && (
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          Project
          <select
            aria-label="Project"
            className="h-7 rounded-md border bg-background px-2 text-xs text-foreground"
            value={project ?? ""}
            onChange={(event) => onProjectChange(event.target.value || null)}
          >
            <option value="">All projects</option>
            {projects.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="relative ml-auto w-64">
        <SearchIcon
          aria-hidden
          className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          ref={searchRef}
          type="search"
          aria-label="Search agents"
          placeholder="Search agents  /"
          className="h-7 pr-7 pl-7 text-xs [&::-webkit-search-cancel-button]:appearance-none"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              onQueryChange("");
              event.currentTarget.blur();
            }
          }}
        />
        {query && (
          <button
            type="button"
            aria-label="Clear search"
            className="absolute top-1/2 right-2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            onClick={() => onQueryChange("")}
          >
            <XIcon className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
