/**
 * User search/autocomplete hook for the permissions "add user" field.
 *
 * Inversion of control: the actual search logic is supplied by the host via
 * `OmnigentHostConfig.searchUsers` (see `lib/host.ts`). When no host searcher
 * is configured (standalone, or before the host wires it), this hook is inert —
 * it never fetches and returns no suggestions, so callers fall back to a plain
 * text input.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getOmnigentUserSearch, type UserSuggestion } from "@/lib/host";

const DEBOUNCE_MS = 300;

interface UseUserSearchResult {
  suggestions: UserSuggestion[];
  isLoading: boolean;
  /** True when a host searcher is configured (the combobox should be shown). */
  enabled: boolean;
}

export function useUserSearch(query: string): UseUserSearchResult {
  const searchUsers = getOmnigentUserSearch();

  // Debounce the typed query so we don't fire a request per keystroke.
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query), DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [query]);

  const trimmed = debouncedQuery.trim();
  const enabled = !!searchUsers && trimmed.length > 0;

  const { data, isFetching } = useQuery({
    queryKey: ["omnigentUserSearch", trimmed],
    queryFn: ({ signal }) => searchUsers!(trimmed, { signal }),
    enabled,
  });

  return {
    suggestions: data ?? [],
    isLoading: enabled && isFetching,
    enabled: !!searchUsers,
  };
}
