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
import { PICKER_VISIBILITY_ENTRY } from "@/pages/settings/search/catalog/appearance";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import {
  settingsControlProps,
  settingsDescriptionId,
  settingsLabelId,
} from "@/pages/settings/search/settingsControl";
import {
  settingsOverlayProps,
  settingsRuntimeChoiceProps,
} from "@/pages/settings/search/settingsOwnership";

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
    <SettingsItem
      entry={PICKER_VISIBILITY_ENTRY}
      className="flex items-start justify-between gap-6"
    >
      <div className="flex min-w-0 flex-col">
        <span id={settingsLabelId(PICKER_VISIBILITY_ENTRY)} className="text-ui font-medium">
          Show in agent pickers
        </span>
        <span
          id={settingsDescriptionId(PICKER_VISIBILITY_ENTRY)}
          className="text-sm text-muted-foreground"
        >
          Choose this server's harnesses and agents for new chats, automations, and project
          defaults. Existing sessions stay unchanged; the selected entry stays visible.
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
            {...settingsControlProps(PICKER_VISIBILITY_ENTRY, "retry")}
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
              {...settingsControlProps(PICKER_VISIBILITY_ENTRY, "edit")}
              data-testid="picker-visibility-edit"
              componentId="settings.appearance.picker_visibility_edit"
            >
              Edit
            </Button>
          </PopoverTrigger>
          <PopoverContent
            {...settingsOverlayProps(PICKER_VISIBILITY_ENTRY, "edit")}
            align="end"
            className="p-0"
          >
            <Command
              label="Filter harnesses and agents"
              {...settingsControlProps(PICKER_VISIBILITY_ENTRY, "filter")}
            >
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
                          {...settingsRuntimeChoiceProps(PICKER_VISIBILITY_ENTRY, "edit")}
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
          {...settingsControlProps(PICKER_VISIBILITY_ENTRY, "showAll")}
          data-testid="picker-visibility-reset"
          componentId="settings.appearance.picker_visibility_reset"
        >
          Show all
        </Button>
      </div>
    </SettingsItem>
  );
}
