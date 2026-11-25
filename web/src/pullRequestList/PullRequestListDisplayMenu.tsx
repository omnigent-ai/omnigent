// The new Pull Requests list's Display popover: how rows group and order, and a way back to
// the defaults. Changes apply as they're made.

import { ArrowDownIcon, ArrowUpIcon, SlidersHorizontalIcon } from "lucide-react";
import { useId } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  pageGroupings,
  PAGE_SORTS,
  pageSortAvailable,
  type pageSortOptions,
  type PageDisplay,
  type PageGrouping,
  type PageSort,
  type PageSortKey,
} from "./pullRequestListPageModel";

export function PullRequestListDisplayMenu({
  display,
  modified,
  sortOptions = PAGE_SORTS,
  sort,
  statsReady,
  onChange,
  onReset,
  onReverse,
  subAgents,
  subAgentPullRequests = false,
}: {
  /** The display shown. */
  display: PageDisplay;
  /** Whether the user picked another display than the default, so Reset has something to do. */
  modified: boolean;
  /** The orders the list offers, a search's own among them where its rows come in one. */
  sortOptions?: ReturnType<typeof pageSortOptions>;
  /** The order the list applies, which falls back to status while stats are unavailable. */
  sort: PageSort;
  /** Update-time and size orders need the desktop shell's stats. */
  statsReady: boolean;
  onChange: (display: PageDisplay) => void;
  /** Back to the list's own display. */
  onReset: () => void;
  /** Flips the applied order, shared with ⌘K. */
  onReverse: () => void;
  /** Whether the list shows sub-agents' PRs, when the list belongs to a session. */
  subAgents?: { included: boolean; onChange: (included: boolean) => void };
  /** Whether the list belongs to a session, so it groups by sub-agent. */
  subAgentPullRequests?: boolean;
}) {
  const ascending = sort.direction === "ascending";
  const subAgentSwitchId = useId();
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={`pr-list-display-trigger${modified ? " modified" : ""}`}
          aria-label="Display Options"
        >
          <SlidersHorizontalIcon aria-hidden />
          <span>Display</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={6} className="pr-list-portal pr-list-popover">
        <div className="pr-list-display-option">
          <span>Grouping</span>
          <Select
            value={display.grouping}
            onValueChange={(grouping) =>
              onChange({ ...display, grouping: grouping as PageGrouping })
            }
          >
            <SelectTrigger size="sm" aria-label="Group by">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {pageGroupings(subAgentPullRequests).map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.id === "none" ? "None" : option.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="pr-list-display-option">
          <span>Ordering</span>
          <div className="pr-list-ordering">
            <Select
              value={sort.key}
              onValueChange={(value) => {
                const option = sortOptions.find((candidate) => candidate.id === value);
                if (!option) return;
                onChange({
                  ...display,
                  sort: { key: option.id as PageSortKey, direction: option.defaultDirection },
                });
              }}
            >
              <SelectTrigger size="sm" aria-label="Order by" className="min-w-0 flex-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {sortOptions
                  .filter((option) => pageSortAvailable(option.id, statsReady))
                  .map((option) => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.title}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
            <button
              type="button"
              className="pr-list-icon-button"
              aria-label={ascending ? "Ascending" : "Descending"}
              title={ascending ? "Ascending" : "Descending"}
              onClick={onReverse}
            >
              {ascending ? <ArrowUpIcon aria-hidden /> : <ArrowDownIcon aria-hidden />}
            </button>
          </div>
        </div>
        {subAgents && (
          <div className="pr-list-display-option pr-list-display-switch">
            <label htmlFor={subAgentSwitchId}>Include sub-agent pull requests</label>
            <Switch
              id={subAgentSwitchId}
              checked={subAgents.included}
              onCheckedChange={subAgents.onChange}
            />
          </div>
        )}
        {modified && (
          <button type="button" className="pr-list-reset" onClick={onReset}>
            Reset to Default
          </button>
        )}
      </PopoverContent>
    </Popover>
  );
}
