import { useMemo, useState } from "react";

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
import { useAgentCatalog, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHiddenPickerAgents } from "@/hooks/useHiddenPickerAgents";
import {
  partitionAgentsByKind,
  partitionPickerEntries,
  selectableSessionAgents,
} from "@/lib/agentGrouping";
import { resetHiddenPickerAgents, setPickerAgentHidden } from "@/lib/pickerEntryVisibility";

const NAMED_HIDDEN_LIMIT = 3;
const LIST_FORMAT = new Intl.ListFormat("en", { style: "long", type: "conjunction" });

/**
 * Describe what the agent pickers show, e.g. "Showing 15 of 17. Hidden: Debby and Codex."
 *
 * @param entries - Every catalog entry the pickers can offer.
 * @param hidden - Stored hidden names; names no longer in the catalog are ignored.
 * @returns The one-line summary.
 */
export function pickerVisibilitySummary(
  entries: readonly AvailableAgent[],
  hidden: ReadonlySet<string>,
): string {
  const hiddenNames = entries
    .filter((entry) => hidden.has(entry.name))
    .map((entry) => entry.display_name);
  if (hiddenNames.length === 0) return `Showing all ${entries.length}.`;
  const named = hiddenNames.slice(0, NAMED_HIDDEN_LIMIT);
  const overflow = hiddenNames.length - named.length;
  const listed = LIST_FORMAT.format(overflow > 0 ? [...named, `${overflow} more`] : named);
  return `Showing ${entries.length - hiddenNames.length} of ${entries.length}. Hidden: ${listed}.`;
}

/** Per-device visibility of entries in the shared agent pickers. */
export function PickerVisibilitySetting() {
  const hidden = useHiddenPickerAgents();
  const { data: agents, isLoading, isError, retry } = useAgentCatalog();
  const [open, setOpen] = useState(false);
  const groups = useMemo(() => {
    const selectable = selectableSessionAgents(agents ?? []);
    const { harnesses, agents: rest } = partitionPickerEntries(selectable);
    const { builtins, customs } = partitionAgentsByKind(rest);
    return [
      { label: "Harnesses", entries: harnesses },
      { label: "Agents", entries: [...builtins, ...customs] },
    ].filter((group) => group.entries.length > 0);
  }, [agents]);
  const entries = useMemo(() => groups.flatMap((group) => group.entries), [groups]);
  const anyHidden = entries.some((entry) => hidden.has(entry.name));
  const loadFailed = entries.length === 0 && isError;

  const summary =
    entries.length > 0
      ? pickerVisibilitySummary(entries, hidden)
      : isLoading
        ? "Loading agents…"
        : loadFailed
          ? "Couldn't load this server's harnesses and agents."
          : "No agents available on this server yet.";

  return (
    <div className="flex items-start justify-between gap-6">
      <div className="flex min-w-0 flex-col">
        <span className="text-ui font-medium">Show in agent pickers</span>
        <span className="text-sm text-muted-foreground">
          Choose which of this server's harnesses and agents appear when you start a chat, create an
          automation or set a project default. Existing sessions are unaffected, and the entry you
          have selected always stays visible.
        </span>
        <span className="mt-1 text-sm" data-testid="picker-visibility-summary">
          {summary}
        </span>
      </div>
      <div className="mt-0.5 flex shrink-0 items-center gap-1">
        {loadFailed && (
          <Button
            variant="ghost"
            size="sm"
            onClick={retry}
            data-testid="picker-visibility-retry"
            componentId="settings.appearance.picker_visibility_retry"
          >
            Retry
          </Button>
        )}
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              disabled={entries.length === 0}
              data-testid="picker-visibility-edit"
              componentId="settings.appearance.picker_visibility_edit"
            >
              Edit
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="p-0">
            <Command label="Filter harnesses and agents">
              <CommandInput placeholder="Filter harnesses and agents…" />
              <CommandList>
                <CommandEmpty>No entries match.</CommandEmpty>
                {groups.map((group) => (
                  <CommandGroup key={group.label} heading={group.label}>
                    {group.entries.map((entry) => {
                      const shown = !hidden.has(entry.name);
                      return (
                        <CommandItem
                          key={entry.id}
                          value={`${entry.display_name} ${entry.id}`}
                          keywords={[entry.name]}
                          data-checked={shown}
                          aria-checked={shown}
                          data-testid={`picker-visibility-toggle-${entry.name}`}
                          onSelect={() => setPickerAgentHidden(entry.name, shown)}
                        >
                          <span className="min-w-0 flex-1 truncate">{entry.display_name}</span>
                        </CommandItem>
                      );
                    })}
                  </CommandGroup>
                ))}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
        <Button
          variant="ghost"
          size="sm"
          disabled={!anyHidden}
          onClick={resetHiddenPickerAgents}
          data-testid="picker-visibility-reset"
          componentId="settings.appearance.picker_visibility_reset"
        >
          Show all
        </Button>
      </div>
    </div>
  );
}
