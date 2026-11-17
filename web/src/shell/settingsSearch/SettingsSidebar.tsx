// The sidebar Settings shows: Back, the search field, then the section nav, which a typed query's
// results replace. The query lasts while Settings stays open, across sections.

import { useState } from "react";

import { SettingsSidebarBody } from "@/shell/settingsNav";

import { SettingsSearch } from "./SettingsSearch";

export function SettingsSidebar({
  onNavClick,
  harnessesOffered,
  onNavigate,
}: {
  onNavClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  harnessesOffered?: boolean;
  /** After a search result opens its row, such as to close the phone's Settings menu. */
  onNavigate: () => void;
}) {
  const [query, setQuery] = useState("");
  return (
    <SettingsSidebarBody
      onNavClick={onNavClick}
      harnessesOffered={harnessesOffered}
      searching={query.trim() !== ""}
      search={<SettingsSearch query={query} onQueryChange={setQuery} onNavigate={onNavigate} />}
    />
  );
}
