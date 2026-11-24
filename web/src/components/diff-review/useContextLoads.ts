// Loading a patch's full text so its unchanged lines can expand. One load per
// patch at a time, its state for the file's separators and header, and the
// patch with its full text once it arrives. A new loader (another pull request,
// head or source) starts over, and what the old one finishes is dropped.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  hydratePartialDiff,
  type FileDiffContentsLoader,
  type FileDiffLoadedFiles,
  type FileDiffMetadata,
} from "@pierre/diffs";
import { type ContextLoadState, markContextLoads } from "./contextLoadIndicator";
import { fullTextMatchesPatch } from "./fullTextMatchesPatch";

/** The loaded text isn't the text the patch was made from, so the file keeps only its patch. */
export class MismatchedFullTextError extends Error {
  constructor() {
    super("The full file doesn't match this diff.");
    this.name = "MismatchedFullTextError";
  }
}

/** A load finished after its loader was replaced or its view closed; nothing may use it. */
export class StaleContextLoadError extends Error {
  constructor() {
    super("These lines were loaded for a diff that's no longer shown.");
    this.name = "StaleContextLoadError";
  }
}

interface ContextLoadStore {
  loader: FileDiffContentsLoader | undefined;
  states: ReadonlyMap<FileDiffMetadata, ContextLoadState>;
  /** Each patch, by its parsed object, with its full text. */
  hydrated: ReadonlyMap<FileDiffMetadata, FileDiffMetadata>;
}

const emptyStore = (loader: FileDiffContentsLoader | undefined): ContextLoadStore => ({
  loader,
  states: new Map(),
  hydrated: new Map(),
});

export interface ContextLoads {
  /** Load a patch's full text; pass it to the renderer as its `loadDiffFiles`. */
  load: FileDiffContentsLoader | undefined;
  states: ReadonlyMap<FileDiffMetadata, ContextLoadState>;
  /** The patch with its full text, once a load for it succeeded. */
  hydrated: (fileDiff: FileDiffMetadata) => FileDiffMetadata | undefined;
  /** Load a patch again after a failure; the reader's expansion stays as it was. */
  retry: (fileDiff: FileDiffMetadata) => void;
}

export function useContextLoads(loader: FileDiffContentsLoader | undefined): ContextLoads {
  const [store, setStore] = useState(() => emptyStore(loader));
  let current = store;
  if (store.loader !== loader) {
    current = emptyStore(loader);
    setStore(current);
  }
  // Only the mounted view's current loader may deliver what it loads.
  const live = useRef<FileDiffContentsLoader | undefined>(loader);
  useEffect(() => {
    live.current = loader;
    return () => {
      live.current = undefined;
    };
  }, [loader]);

  const load = useMemo(() => {
    if (!loader) return undefined;
    const pending = new WeakMap<FileDiffMetadata, Promise<FileDiffLoadedFiles>>();
    const update = (change: (store: ContextLoadStore) => ContextLoadStore) =>
      setStore((latest) => (latest.loader === loader ? change(latest) : latest));
    const withState = (fileDiff: FileDiffMetadata, state: ContextLoadState | undefined) =>
      update((latest) => {
        if (latest.states.get(fileDiff) === state) return latest;
        const states = new Map(latest.states);
        if (state) states.set(fileDiff, state);
        else states.delete(fileDiff);
        return { ...latest, states };
      });
    return (fileDiff: FileDiffMetadata) => {
      const known = pending.get(fileDiff);
      if (known) return known;
      // A stale result rejects, so the renderer awaiting it doesn't take it either.
      const loading = loader(fileDiff).then((files) => {
        if (live.current !== loader) throw new StaleContextLoadError();
        if (!fullTextMatchesPatch(fileDiff, files)) throw new MismatchedFullTextError();
        return files;
      });
      pending.set(fileDiff, loading);
      withState(fileDiff, "loading");
      loading.then(
        (files) => {
          withState(fileDiff, undefined);
          // The renderer may already have merged the text into this very patch.
          if (!fileDiff.isPartial) return;
          const loaded = hydratePartialDiff("clone", fileDiff, files);
          update((latest) => ({
            ...latest,
            hydrated: new Map(latest.hydrated).set(fileDiff, loaded),
          }));
        },
        (error: unknown) => {
          if (error instanceof StaleContextLoadError) {
            pending.delete(fileDiff);
          } else if (error instanceof MismatchedFullTextError) {
            // Loading again reads the same text, so this patch keeps the answer.
            withState(fileDiff, "mismatched");
          } else {
            // The next expansion, reveal or Retry asks again.
            pending.delete(fileDiff);
            withState(fileDiff, "failed");
          }
        },
      );
      return loading;
    };
  }, [loader]);

  const { states, hydrated: hydratedDiffs } = current;
  const hydrated = useCallback(
    (fileDiff: FileDiffMetadata) => hydratedDiffs.get(fileDiff),
    [hydratedDiffs],
  );
  const retry = useCallback(
    (fileDiff: FileDiffMetadata) => {
      // A failure shows as the file's state; nothing else to do with it here.
      void load?.(fileDiff).catch(() => undefined);
    },
    [load],
  );
  return { load, states, hydrated, retry };
}

/**
 * One stacked file's part: its load state, the diff to show (with its full text
 * once a Retry loaded it), and a marker for its rendered host. The renderer's own
 * loads fill the patch in place, so only a Retry needs the loaded copy.
 */
export function useFileContextLoad(
  path: string,
  fileDiff: FileDiffMetadata | undefined,
  contextLoads: ContextLoads | undefined,
  section: { readonly current: HTMLElement | null },
) {
  const state = fileDiff ? contextLoads?.states.get(fileDiff) : undefined;
  const shownDiff = (fileDiff && contextLoads?.hydrated(fileDiff)) ?? fileDiff;
  const mark = useCallback(() => {
    const host = section.current?.querySelector("diffs-container");
    if (host) markContextLoads([{ id: path, element: host }], () => state);
  }, [path, state, section]);
  useEffect(() => mark(), [mark, shownDiff]);
  const retry = useCallback(() => {
    if (fileDiff) contextLoads?.retry(fileDiff);
  }, [fileDiff, contextLoads]);
  return { state, shownDiff, mark, retry };
}
