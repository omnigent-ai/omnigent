import type { Conversation } from "@/hooks/useConversations";
import type { LoadedRowsSnapshot } from "@/hooks/useSidebarData";

type LoadedPages = { pages: { data: readonly unknown[] }[] } | null | undefined;

/**
 * Selector hooks for a test that mocks `@/hooks/useSidebarData`: they read the
 * same pages the mock's `useOptionalLoadedConversations` returns. The flattened
 * rows stay the same array until `pages` changes, as in the real provider.
 *
 * Spread the result into the mock:
 * `...(await import("@/test/loadedRowsMock")).loadedRowsSelectorMocks(() => pages)`.
 */
export function loadedRowsSelectorMocks(pages: () => LoadedPages) {
  let cached: { source: LoadedPages; rows: readonly Conversation[] | undefined } | null = null;
  const rows = () => {
    const source = pages();
    if (!cached || cached.source !== source) {
      cached = {
        source,
        rows: source?.pages.flatMap((page) => page.data as readonly Conversation[]),
      };
    }
    return cached.rows;
  };
  return {
    useLoadedConversationsSelector: <T>(select: (snapshot: LoadedRowsSnapshot) => T): T =>
      select({ rows: rows(), isLoading: false }),
    useLoadedConversation: (id: string | null | undefined): Conversation | undefined =>
      id ? rows()?.find((row) => row.id === id) : undefined,
  };
}
