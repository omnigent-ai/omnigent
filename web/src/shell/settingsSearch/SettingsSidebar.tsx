// The search field above Settings' section nav. A typed query's results take the nav's place,
// and clearing the query brings the nav back. The query lasts while Settings stays open.

import { type ReactNode, useState } from "react";

import { SettingsSearch } from "./SettingsSearch";

export function SettingsSidebar({
  nav,
  onNavigate,
}: {
  /** The section nav, shown while the query is empty. */
  nav: ReactNode;
  /** After a search result opens its row, such as to close the phone's Settings menu. */
  onNavigate: () => void;
}) {
  const [query, setQuery] = useState("");
  return (
    <>
      <SettingsSearch query={query} onQueryChange={setQuery} onNavigate={onNavigate} />
      {query.trim() === "" ? nav : null}
    </>
  );
}
