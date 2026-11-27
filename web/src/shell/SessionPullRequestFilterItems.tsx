// The sidebar filter menu's pull request choices, laid out as its owner choices are.

import { DropdownMenuRadioGroup, DropdownMenuRadioItem } from "@/components/ui/dropdown-menu";
import {
  SESSION_PULL_REQUEST_FILTER_LABELS,
  SESSION_PULL_REQUEST_FILTERS,
  type SessionPullRequestFilter,
} from "@/lib/sessionListFilters";

export function SessionPullRequestFilterItems({
  value,
  onChange,
}: {
  value: SessionPullRequestFilter;
  onChange: (value: SessionPullRequestFilter) => void;
}) {
  return (
    <DropdownMenuRadioGroup
      value={value}
      onValueChange={(next) => {
        const filter = SESSION_PULL_REQUEST_FILTERS.find((choice) => choice === next);
        if (filter) onChange(filter);
      }}
    >
      {SESSION_PULL_REQUEST_FILTERS.map((filter) => (
        <DropdownMenuRadioItem
          className="mobile-touch-target"
          key={filter}
          value={filter}
          data-testid={`session-pull-request-filter-${filter}`}
        >
          {SESSION_PULL_REQUEST_FILTER_LABELS[filter]}
        </DropdownMenuRadioItem>
      ))}
    </DropdownMenuRadioGroup>
  );
}
