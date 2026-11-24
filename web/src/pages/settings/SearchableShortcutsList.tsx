// The Keyboard shortcuts page's list, with a field that filters it by name, group or keys.

import { SearchIcon } from "lucide-react";
import { useLayoutEffect, useMemo, useState } from "react";

import { KeyboardShortcutsList, useShortcutPageGroups } from "@/components/KeyboardShortcutsDialog";
import { useLocation } from "@/lib/routing";
import { currentShortcutEnvironment } from "@/lib/shortcuts/registry";

import { SHORTCUTS_SEARCH_ENTRY } from "./search/catalog/shortcuts";
import { SettingsItem } from "./search/SettingsItem";
import { parseSettingsHash, settingsControlProps } from "./search/settingsControl";
import { filterShortcutPage } from "./shortcutsFilter";

export function SearchableShortcutsList() {
  const [query, setQuery] = useState("");
  const groups = useShortcutPageGroups();
  const shown = useMemo(
    () => filterShortcutPage(groups, query, currentShortcutEnvironment()),
    [groups, query],
  );
  // A link to one shortcut, such as a Settings search result, shows every row so it can be revealed.
  const location = useLocation();
  useLayoutEffect(() => {
    if (parseSettingsHash(location.hash)) setQuery("");
  }, [location]);

  return (
    <div className="flex flex-col gap-6">
      <SettingsItem entry={SHORTCUTS_SEARCH_ENTRY}>
        <span className="sr-only">{SHORTCUTS_SEARCH_ENTRY.label}</span>
        <div className="flex h-8 items-center gap-2 rounded-lg border border-border px-2.5">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape" && query !== "") {
                event.preventDefault();
                setQuery("");
              }
            }}
            {...settingsControlProps(SHORTCUTS_SEARCH_ENTRY, "search")}
            placeholder="Search shortcuts by name or keys"
            data-testid="shortcuts-search"
            className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
          />
        </div>
      </SettingsItem>
      {shown.length > 0 ? (
        <KeyboardShortcutsList variant="settings" groups={shown} />
      ) : (
        <p role="status" className="py-6 text-center text-ui text-muted-foreground">
          No shortcuts match “{query.trim()}”
        </p>
      )}
    </div>
  );
}
