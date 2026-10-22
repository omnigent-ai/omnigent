import { useCallback, useContext, useEffect, useRef, useState, type RefObject } from "react";
import { Loader2Icon } from "lucide-react";
import { SidebarConfigContext } from "@/lib/sidebarConfig";
import { cn } from "@/lib/utils";

export interface AutoLoadBudget {
  scope: string;
  count: number;
}

export function InfiniteScrollSentinel({
  hasMore,
  isFetching,
  fetchMore,
  scrollRoot,
  indent,
  scopeKey = "sessions",
  maxAutoLoads,
  budgetRef,
}: {
  hasMore: boolean;
  isFetching: boolean;
  fetchMore: () => unknown;
  scrollRoot: RefObject<HTMLElement | null>;
  indent?: boolean;
  scopeKey?: string;
  maxAutoLoads?: number;
  budgetRef?: RefObject<AutoLoadBudget>;
}) {
  const config = useContext(SidebarConfigContext);
  const limit = maxAutoLoads ?? config.maxAutoLoads;
  const localBudget = useRef<AutoLoadBudget>({ scope: scopeKey, count: 0 });
  const budget = budgetRef ?? localBudget;
  if (budget.current.scope !== scopeKey) budget.current = { scope: scopeKey, count: 0 };
  const [revision, setRevision] = useState(0);
  const ref = useRef<HTMLButtonElement>(null);
  const inFlight = useRef(false);
  const load = useCallback(
    (automatic: boolean) => {
      if (!hasMore || isFetching || inFlight.current) return;
      if (automatic && budget.current.count >= limit) return;
      if (automatic) budget.current.count += 1;
      inFlight.current = true;
      let pending: unknown;
      try {
        pending = fetchMore();
      } catch {
        pending = undefined;
      }
      void Promise.resolve(pending)
        .catch(() => {})
        .finally(() => {
          inFlight.current = false;
          setRevision((value) => value + 1);
        });
    },
    [hasMore, isFetching, fetchMore, budget, limit],
  );
  useEffect(() => {
    const element = ref.current;
    if (!element || !hasMore || budget.current.count >= limit) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) load(true);
      },
      { root: scrollRoot.current, rootMargin: "200px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasMore, budget, limit, load, scrollRoot, revision, scopeKey]);
  if (!hasMore) return null;
  return (
    <button
      ref={ref}
      type="button"
      disabled={isFetching}
      onClick={() => load(false)}
      className={cn(
        "flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-muted-foreground text-sm hover:bg-muted disabled:pointer-events-none disabled:opacity-50",
        indent && "pl-5",
      )}
    >
      {isFetching ? (
        <>
          <Loader2Icon className="size-3 animate-spin" />
          Loading…
        </>
      ) : (
        "Load more"
      )}
    </button>
  );
}
