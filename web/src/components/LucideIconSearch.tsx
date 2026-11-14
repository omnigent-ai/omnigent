// The searchable lucide grid in the project mark picker. Lazy-loaded with the
// catalog (see ProjectIconPicker), so its icons stay out of the main bundle.

import { useState } from "react";
import { FolderIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import { lucideIcon, lucideIconLabel, searchLucideIcons } from "@/lib/lucideIconCatalog";
import { cn } from "@/lib/utils";

// Drawing every icon at once is slow on phones; typing narrows the rest.
const RESULT_LIMIT = 200;

const CELL_CLASS =
  "flex size-8 cursor-pointer items-center justify-center rounded-md outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring/40";

/** Calls `onSelect` with the picked icon name, or `null` for the default folder. */
export function LucideIconSearch({
  selected,
  color,
  onSelect,
}: {
  selected: string | null;
  color: string | undefined;
  onSelect: (name: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  const results = searchLucideIcons(query);
  // Unfiltered, lead with the current pick so it shows past the first page.
  const matches =
    !query && selected && lucideIcon(selected)
      ? [selected, ...results.filter((name) => name !== selected)]
      : results;
  const shown = matches.slice(0, RESULT_LIMIT);

  return (
    <div className="flex w-[296px] flex-col gap-2" data-testid="lucide-icon-search">
      <Input
        type="search"
        aria-label="Search icons"
        placeholder="Search icons"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        autoFocus
      />
      <div className="grid max-h-64 grid-cols-8 gap-1 overflow-y-auto">
        {!query && (
          <button
            type="button"
            aria-label="Default folder"
            title="Default folder"
            aria-pressed={selected === null}
            data-testid="lucide-icon-default"
            onClick={() => onSelect(null)}
            className={cn(CELL_CLASS, selected === null && "bg-muted")}
          >
            <FolderIcon className="size-4" style={{ color }} />
          </button>
        )}
        {shown.map((name) => {
          const Icon = lucideIcon(name);
          if (!Icon) return null;
          const label = lucideIconLabel(name);
          return (
            <button
              key={name}
              type="button"
              aria-label={label}
              title={label}
              aria-pressed={selected === name}
              data-testid={`lucide-icon-${name}`}
              onClick={() => onSelect(name)}
              className={cn(CELL_CLASS, selected === name && "bg-muted")}
            >
              <Icon className="size-4" style={{ color }} />
            </button>
          );
        })}
      </div>
      {matches.length === 0 && (
        <p className="px-1 text-sm text-muted-foreground">No icons match.</p>
      )}
      {matches.length > shown.length && (
        <p className="px-1 text-xs text-muted-foreground">
          Showing {shown.length} of {matches.length}. Keep typing to narrow.
        </p>
      )}
    </div>
  );
}
