// One search field over every Settings row and keyboard shortcut. Picking a result opens its
// section and reveals the row (or part) itself; the query stays for the next pick.

import { useMemo, useRef, type KeyboardEvent } from "react";

import { CompactShortcutKeys } from "@/components/ui/kbd";
import type { QuickPickItem } from "@/components/ui/quick-pick/model";
import type { QuickPickSegment } from "@/components/ui/quick-pick/presentation";
import { QuickPick } from "@/components/ui/quick-pick/QuickPick";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useNavigate } from "@/lib/routing";
import type { ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";
import {
  searchSettings,
  settingsSearchPath,
  type SettingsSearchTarget,
} from "@/pages/settings/search/settingsSearchIndex";

import { useSettingsSearchFocusTarget } from "./settingsSearchFocus";
import { useSettingsSearchTargets } from "./useSettingsSearchTargets";

export interface SettingsSearchProps {
  query: string;
  onQueryChange: (query: string) => void;
  /** After a result opens its row, such as to close the phone's Settings menu. */
  onNavigate?: () => void;
}

/** The best matches shown, few enough that a full-height window lists them without scrolling. */
export const SETTINGS_SEARCH_RESULT_LIMIT = 10;

/** A shortcut's key caps, compact beside its name; its alternative follows "or". */
function ShortcutKeys({ row }: { row: ShortcutPageRow }) {
  return (
    <span className="flex shrink-0 items-center gap-1">
      <CompactShortcutKeys keys={row.keys} />
      {row.alternateKeys ? (
        <>
          <span className="text-10 text-muted-foreground/70">or</span>
          <CompactShortcutKeys keys={row.alternateKeys} />
        </>
      ) : null}
    </span>
  );
}

export function SettingsSearch({ query, onQueryChange, onNavigate }: SettingsSearchProps) {
  const fieldRef = useRef<HTMLInputElement | null>(null);
  useSettingsSearchFocusTarget(fieldRef);
  const navigate = useNavigate();
  const { targets, sections, shortcutRows, matchers } = useSettingsSearchTargets();

  const segments = useMemo<QuickPickSegment<SettingsSearchTarget>[]>(
    () => [
      {
        sourceId: "settings-search",
        // Settings ranks its own results, so a chord can outrank text, and each row shows once.
        ranking: "provider",
        items: searchSettings(query, targets, matchers)
          .slice(0, SETTINGS_SEARCH_RESULT_LIMIT)
          .map(({ target, labelRanges, placeRanges }): QuickPickItem<SettingsSearchTarget> => {
            const Icon = sections.get(target.entry.section)?.icon;
            const shortcutRow =
              target.entry.shortcutId === undefined
                ? undefined
                : shortcutRows.get(target.entry.shortcutId);
            return {
              id: target.key,
              label: target.label,
              description: target.place,
              icon: Icon ? <Icon /> : undefined,
              accessory: shortcutRow ? <ShortcutKeys row={shortcutRow} /> : undefined,
              highlights: { label: labelRanges, description: placeRanges },
              value: target,
            };
          }),
      },
    ],
    [query, targets, sections, shortcutRows, matchers],
  );

  // Escape clears the query first, then leaves the field. Handled on the wrapper, so it also
  // works from the clear button, which takes focus.
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || isImeCompositionKeyEvent(event)) return;
    if (query !== "") {
      onQueryChange("");
      fieldRef.current?.focus();
    } else {
      fieldRef.current?.blur();
    }
  };

  return (
    <div onKeyDown={handleKeyDown}>
      <QuickPick<SettingsSearchTarget>
        label="Search settings"
        placeholder="Search settings"
        presentation="sidebar"
        segments={segments}
        query={query}
        onQueryChange={onQueryChange}
        inputRef={fieldRef}
        autoFocus={false}
        // Empty, the search is just its field, and the Settings nav below takes the height.
        fillHeight={query.trim() !== ""}
        emptyMessage={`No settings match “${query.trim()}”`}
        // Every result underlines what matched, as ⌘K's results do, not only the active row.
        matchHighlight="underline"
        className="bg-transparent px-3 pt-1"
        onAccept={(item) => {
          navigate(settingsSearchPath(item.value));
          onNavigate?.();
        }}
      />
    </div>
  );
}
