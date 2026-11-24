import { useInfiniteQuery } from "@tanstack/react-query";
import { LayersIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { queryWorkView, type ViewDefinition, type ViewRecord } from "@/lib/workViewsApi";
import { ResultsFooter, ViewResults } from "./ViewResults";

export function QueriedViewResults({
  definition,
  onOpen,
  notice,
  emptyMessage = "Try adjusting your filters.",
}: {
  definition: ViewDefinition;
  onOpen: (record: ViewRecord) => void;
  notice?: string;
  emptyMessage?: string;
}) {
  const settledSearch = useDebouncedValue(definition.filters.search, 200);
  const queryDefinition = {
    ...definition,
    filters: { ...definition.filters, search: settledSearch },
  };
  const results = useInfiniteQuery({
    queryKey: [
      "view-results",
      definition.entity_type,
      queryDefinition.filters,
      definition.display.order_by,
      definition.display.direction,
    ],
    queryFn: ({ pageParam, signal }) => queryWorkView(queryDefinition, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: definition.entity_type === "sessions" ? 15000 : false,
  });
  const records = [
    ...new Map(
      (results.data?.pages.flatMap((page) => page.data) ?? []).map((record) => [record.id, record]),
    ).values(),
  ];
  return (
    <>
      <div className="mb-3 flex min-h-8 items-center px-1 text-xs text-muted-foreground">
        {results.isFetching && !results.isPending ? "Updating…" : notice}
      </div>
      {results.isPending ? (
        <div className="flex justify-center p-12">
          <Spinner aria-label="Loading results" />
        </div>
      ) : results.error ? (
        <div role="alert" className="p-8 text-sm text-destructive">
          {results.error.message}
          <Button variant="ghost" onClick={() => void results.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <>
          {records.length === 0 && (
            <div className="space-y-2 py-14 text-center">
              <LayersIcon className="mx-auto size-7 text-muted-foreground/50" />
              <p className="text-sm font-medium">No matching {definition.entity_type}</p>
              <p className="text-sm text-muted-foreground">
                {results.hasNextPage
                  ? "More results are available to search. Load more to continue."
                  : emptyMessage}
              </p>
            </div>
          )}
          <ViewResults records={records} definition={definition} onOpen={onOpen} />
          <ResultsFooter
            count={records.length}
            hasMore={results.hasNextPage}
            loading={results.isFetchingNextPage}
            onMore={() => void results.fetchNextPage()}
          />
        </>
      )}
    </>
  );
}
