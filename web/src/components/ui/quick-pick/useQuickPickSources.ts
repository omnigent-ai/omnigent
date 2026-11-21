import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { QuickPickItem, QuickPickSource, QuickPickSourceState } from "./model";

export const SOURCE_LOAD_ERROR_MESSAGE = "Couldn't load results. Try again.";

type SettledState<Value> = Omit<QuickPickSourceState<Value>, "items"> & {
  /** Omitted to keep the previous results. */
  items?: QuickPickSourceState<Value>["items"];
};

interface ActiveRequest {
  requestKey: string;
  query: string;
  resultKey: string | undefined;
  controller: AbortController;
  timer: ReturnType<typeof setTimeout> | null;
}

function requestKeyFor(source: QuickPickSource<unknown>, query: string): string {
  return JSON.stringify([
    source.reloadKey ?? null,
    source.resultKey ?? null,
    source.queryDependent ? query : null,
  ]);
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : SOURCE_LOAD_ERROR_MESSAGE;
}

// Zone-based telemetry replaces the global `Promise`, so an async function's
// promise can fail `instanceof Promise`; anything with `then` is a pending load.
export function isPromiseLike<Result>(value: unknown): value is PromiseLike<Result> {
  return typeof (value as PromiseLike<Result> | null)?.then === "function";
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

// A loader that gave up on its own, rather than for a newer request, says nothing useful.
function failureMessage(error: unknown): string {
  return isAbort(error) ? SOURCE_LOAD_ERROR_MESSAGE : errorMessage(error);
}

export interface QuickPickSourcesResult<Value> {
  states: Record<string, QuickPickSourceState<Value>>;
  /** Reloads sources whose last load failed. */
  retry: () => void;
}

/**
 * Loads each source for the current query. A newer request aborts the older
 * one, stale responses are ignored, and previous results stay visible while a
 * reload is pending so the list does not jump. A provider-ranked source's
 * results belong to their query, so they clear as soon as the query changes.
 */
export function useQuickPickSources<Value>(
  sources: readonly QuickPickSource<Value>[],
  query: string,
): QuickPickSourcesResult<Value> {
  const [states, setStates] = useState<Record<string, QuickPickSourceState<Value>>>({});
  const [retryCount, setRetryCount] = useState(0);
  const requestsRef = useRef(new Map<string, ActiveRequest>());
  const statesRef = useRef(states);
  statesRef.current = states;

  const retry = useCallback(() => {
    // Forget failed requests so the next pass reloads them.
    for (const [id, state] of Object.entries(statesRef.current)) {
      if (state.status === "error") requestsRef.current.delete(id);
    }
    setRetryCount((count) => count + 1);
  }, []);

  // Layout effect: synchronous sources resolve before the first paint.
  useLayoutEffect(() => {
    const requests = requestsRef.current;
    const liveIds = new Set(sources.map((source) => source.id));
    for (const [id, request] of requests) {
      if (liveIds.has(id)) continue;
      request.controller.abort();
      if (request.timer) clearTimeout(request.timer);
      requests.delete(id);
      setStates(({ [id]: _removed, ...rest }) => rest);
    }

    for (const source of sources) {
      const requestKey = requestKeyFor(source as QuickPickSource<unknown>, query);
      const previous = requests.get(source.id);
      if (previous?.requestKey === requestKey) continue;
      if (previous) {
        previous.controller.abort();
        if (previous.timer) clearTimeout(previous.timer);
      }
      const controller = new AbortController();
      const request: ActiveRequest = {
        requestKey,
        query,
        resultKey: source.resultKey,
        controller,
        timer: null,
      };
      requests.set(source.id, request);

      const settle = (next: SettledState<Value>) => {
        if (controller.signal.aborted) return;
        setStates((current) => ({
          ...current,
          [source.id]: {
            ...next,
            items: next.items ?? current[source.id]?.items ?? [],
          },
        }));
      };

      const run = () => {
        request.timer = null;
        const startedAt = performance.now();
        let settled = false;
        const finish = (next: SettledState<Value>) => {
          settled = true;
          settle(next);
        };
        const publish = (items: readonly QuickPickItem<Value>[]) => {
          if (!settled) settle({ status: "loading", items });
        };
        let result: ReturnType<QuickPickSource<Value>["load"]>;
        try {
          result = source.load({ query, signal: controller.signal, publish });
        } catch (error) {
          finish({ status: "error", error: errorMessage(error) });
          return;
        }
        if (!isPromiseLike<readonly QuickPickItem<Value>[]>(result)) {
          finish({ status: "ready", items: result, elapsedMs: performance.now() - startedAt });
          return;
        }
        // A snapshot published synchronously is already showing; keep it.
        settle({ status: "loading" });
        result.then(
          (items) => finish({ status: "ready", items, elapsedMs: performance.now() - startedAt }),
          (error: unknown) => {
            // A superseded request's rejection is the newer one's business; any other fails.
            if (!controller.signal.aborted) {
              finish({ status: "error", error: failureMessage(error) });
            }
          },
        );
      };

      // Provider-ranked rows can't be refiltered, so a new query hides the old query's
      // rows; rows answering other options are hidden for any source.
      if (
        previous &&
        (previous.resultKey !== source.resultKey ||
          (source.ranking === "provider" && previous.query !== query))
      ) {
        settle({ status: "loading", items: [] });
      }
      const debounceMs = previous ? (source.debounceMs ?? 0) : 0;
      if (debounceMs > 0) {
        settle({ status: "loading" });
        request.timer = setTimeout(run, debounceMs);
      } else {
        run();
      }
    }
  }, [sources, query, retryCount]);

  useEffect(() => {
    const requests = requestsRef.current;
    return () => {
      for (const request of requests.values()) {
        request.controller.abort();
        if (request.timer) clearTimeout(request.timer);
      }
      requests.clear();
    };
  }, []);

  return { states, retry };
}
