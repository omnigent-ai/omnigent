import { type RefObject, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileIcon, SearchIcon } from "lucide-react";
import { authenticatedFetch } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import type { OpenFileOptions } from "./FileViewerContext";

interface ContentMatch {
  path: string;
  line: number;
  column: number;
  text: string;
  match_start: number;
  match_end: number;
}

interface ContentSearchResult {
  matches: ContentMatch[];
  truncated: boolean;
  warning: string | null;
}

interface ContentSearchInput {
  query: string;
  caseSensitive: boolean;
  regex: boolean;
}

export function ContentSearchPanel({
  conversationId,
  onFileSelect,
  inputRef,
}: {
  conversationId: string;
  onFileSelect: (path: string, options?: OpenFileOptions) => void;
  inputRef?: RefObject<HTMLInputElement | null>;
}) {
  const queryClient = useQueryClient();
  const cachedInput = useQuery<ContentSearchInput>({
    queryKey: ["content-search-input", conversationId],
    initialData: { query: "", caseSensitive: false, regex: false },
    enabled: false,
    gcTime: Infinity,
  });
  const [input, setInput] = useState(cachedInput.data);
  const { query, caseSensitive, regex } = input;
  useEffect(() => {
    queryClient.setQueryData(["content-search-input", conversationId], input);
  }, [conversationId, input, queryClient]);
  const search = useQuery({
    queryKey: ["content-search-result", conversationId],
    enabled: false,
    gcTime: Infinity,
    retry: false,
    // Keep the latest search, including in-flight work, when its panel unmounts.
    queryFn: async (): Promise<ContentSearchResult & { query: string }> => {
      const params = new URLSearchParams({
        q: query,
        case_sensitive: String(caseSensitive),
        regex: String(regex),
      });
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/resources/environments/default/content-search?${params}`,
      );
      const body = await response.json().catch(() => null);
      if (!response.ok || body?.error || !Array.isArray(body?.matches)) {
        throw new Error(
          (typeof body?.error === "string" ? body.error : body?.error?.message) ||
            (typeof body?.detail === "string" ? body.detail : null) ||
            "Content search is unavailable. Check that the runner is online and up to date.",
        );
      }
      return { ...body, query };
    },
  });
  const files = new Map<string, ContentMatch[]>();
  for (const match of search.data?.matches ?? []) {
    const matches = files.get(match.path) ?? [];
    matches.push(match);
    files.set(match.path, matches);
  }

  return (
    <section aria-label="Content search" className="flex min-h-0 flex-1 flex-col">
      <form
        className="space-y-2 border-b border-border p-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (query.trim() && !search.isFetching) void search.refetch();
        }}
      >
        <div className="flex gap-2">
          <Input
            ref={inputRef}
            aria-label="Search file contents"
            placeholder="Search file contents"
            type="search"
            maxLength={4096}
            value={query}
            onChange={(event) => setInput({ ...input, query: event.target.value })}
          />
          <Button type="submit" disabled={!query.trim() || search.isFetching}>
            <SearchIcon className="size-4" />
            Search
          </Button>
        </div>
        <div className="flex flex-wrap gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={caseSensitive}
              onChange={(event) => setInput({ ...input, caseSensitive: event.target.checked })}
            />
            Match case
          </label>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={regex}
              onChange={(event) => setInput({ ...input, regex: event.target.checked })}
            />
            Regular expression
          </label>
        </div>
        <p className="text-xs text-muted-foreground">
          Searches workspace contents on disk. Respects readable ignore files; skips binary files
          and .git.
        </p>
      </form>
      <div className="min-h-0 flex-1 overflow-auto p-2" aria-busy={search.isFetching}>
        {search.isFetching && (
          <div role="status" className="flex items-center gap-2 p-2 text-sm">
            <Spinner /> Searching…
          </div>
        )}
        {search.isError && !search.isFetching && (
          <p role="alert" className="p-2 text-sm text-destructive">
            {search.error.message}
          </p>
        )}
        {!search.isFetched && !search.isFetching && (
          <p className="p-2 text-sm text-muted-foreground">Enter text and press Enter to search.</p>
        )}
        {search.isSuccess && !search.isFetching && (
          <>
            <p role="status" className="break-words p-2 text-xs text-muted-foreground">
              {search.data.matches.length === 0
                ? `No matches for “${search.data.query}”.`
                : `${search.data.matches.length} matching lines in ${files.size} files for “${search.data.query}”.`}
            </p>
            {(search.data.truncated || search.data.warning) && (
              <p role="status" className="p-2 text-xs text-muted-foreground">
                {search.data.warning ||
                  `Showing the first ${search.data.matches.length} matching lines. Narrow your query for more results.`}
              </p>
            )}
            {[...files].map(([path, matches]) => (
              <div key={path} className="mb-2">
                <span className="ml-1.5 mb-2 flex items-center gap-1">
                  <FileIcon className="size-4" />
                  <h3 className="break-all text-xs leading-normal font-semibold">{path}</h3>
                </span>
                {matches.map((match) => (
                  <button
                    key={match.line}
                    type="button"
                    aria-label={`Open ${path} at line ${match.line}`}
                    onClick={() => onFileSelect(path, { line: match.line, column: match.column })}
                    className="flex w-full gap-2 rounded px-2 pl-6 py-1 text-left font-mono text-xs hover:bg-muted focus-visible:outline focus-visible:outline-ring cursor-pointer"
                  >
                    <span className="shrink-0 text-muted-foreground">{match.line}</span>
                    <span className="min-w-0 truncate whitespace-pre">
                      {match.text.slice(0, match.match_start)}
                      <mark>{match.text.slice(match.match_start, match.match_end)}</mark>
                      {match.text.slice(match.match_end)}
                    </span>
                  </button>
                ))}
              </div>
            ))}
          </>
        )}
      </div>
    </section>
  );
}
