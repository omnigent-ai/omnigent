// The review's rich Markdown diffs: which files show one, the shared action that
// switches a file between its rich and source diff, and how a rich file appears
// to CodeView and to find.

import type { CodeViewItem, FileDiffLoadedFiles, FileDiffMetadata } from "@pierre/diffs";
import { BookOpenTextIcon, FileCodeIcon, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { pathFromReviewItemId, syntaxLanguage, type DiffReviewFile } from "./diffReviewModel";
import type { ReviewSearchLine } from "./findMatches";
import { createMarkdownDiffComputer } from "./markdownDiffComputation";
import {
  readRichMarkdownPaths,
  useRichMarkdownPaths,
  writeRichMarkdownPath,
} from "./markdownDiffPreference";
import { MarkdownDiffStore, type MarkdownPreviewState } from "./markdownDiffStore";
import type { DiffReviewContent } from "./reviewItems";
import { usePresentedRichPaths } from "./usePresentedRichPaths";

/** The line-0 annotation a rich file's item carries; its row holds the preview. */
export const MARKDOWN_PREVIEW_ANNOTATION = Object.freeze({ markdownPreview: true });

export function isMarkdownPreviewAnnotation(metadata: unknown): boolean {
  return metadata === MARKDOWN_PREVIEW_ANNOTATION;
}

/** Whether `file` can show a rich diff: a text Markdown file whose diff loaded. */
export function canPreviewMarkdown(
  file: DiffReviewFile,
  content: DiffReviewContent | undefined,
): content is Extract<DiffReviewContent, { status: "ready" }> {
  return !file.isBinary && syntaxLanguage(file.path) === "markdown" && content?.status === "ready";
}

/** The one definition of the switch, shared by the header toggle, the ⋯ menu, and ⌘K. */
export interface MarkdownDiffViewAction {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Whether the file shows its rich diff now. */
  rich: boolean;
  run: () => void;
}

/**
 * The switch for `path`, named by what it shows now. Running it flips the reader's latest
 * choice, which can be ahead of what's on screen: a second click before the first shows still
 * goes back.
 */
export function markdownDiffViewAction(
  path: string,
  rich: boolean,
  toggle: (path: string) => void,
): MarkdownDiffViewAction {
  return {
    id: "markdown-diff-view",
    label: rich ? "Show source diff" : "Show rich diff",
    icon: rich ? FileCodeIcon : BookOpenTextIcon,
    rich,
    run: () => toggle(path),
  };
}

const NO_STATES: ReadonlyMap<string, MarkdownPreviewState> = new Map();
const noSubscription = () => () => {};

export interface MarkdownPreviews {
  /**
   * Paths the review shows as a rich diff now: the reader's choice, once it has rendered.
   * Only previewable files show one.
   */
  richPaths: ReadonlySet<string>;
  /** Paths the reader chose to see as a rich diff, which may not show yet. */
  intendedRichPaths: ReadonlySet<string>;
  /** Whether the reader's latest choice for `path` is its rich diff, read when asked. */
  isIntendedRich: (path: string) => boolean;
  states: ReadonlyMap<string, MarkdownPreviewState>;
  setRich: (path: string, rich: boolean) => void;
  request: (path: string, fileDiff: FileDiffMetadata) => void;
  retry: (path: string) => void;
}

/**
 * Rich diffs for one review. `scope` names the review whose choices are
 * remembered; `loadFullText` fills in a patch's unchanged text.
 */
export function useMarkdownPreviews(
  scope: string,
  loadFullText: ((fileDiff: FileDiffMetadata) => Promise<FileDiffLoadedFiles>) | undefined,
): MarkdownPreviews {
  const intendedRichPaths = useRichMarkdownPaths(scope);
  const richPaths = usePresentedRichPaths(scope, intendedRichPaths);
  const [store, setStore] = useState<MarkdownDiffStore | null>(null);
  useEffect(() => {
    const created = new MarkdownDiffStore({ computer: createMarkdownDiffComputer(), loadFullText });
    setStore(created);
    return () => created.dispose();
    // Another review, or another way to load full text, starts from no previews.
  }, [loadFullText, scope]);
  const states = useSyncExternalStore(
    store ? (listener) => store.subscribe(listener) : noSubscription,
    () => store?.states ?? NO_STATES,
    () => NO_STATES,
  );
  const setRich = useCallback(
    (path: string, rich: boolean) => {
      writeRichMarkdownPath(scope, path, rich);
    },
    [scope],
  );
  const request = useCallback(
    (path: string, fileDiff: FileDiffMetadata) => store?.request(path, fileDiff),
    [store],
  );
  const retry = useCallback((path: string) => store?.retry(path), [store]);
  const isIntendedRich = useCallback(
    (path: string) => readRichMarkdownPaths(scope).has(path),
    [scope],
  );
  return useMemo(
    () => ({ richPaths, intendedRichPaths, isIntendedRich, states, setRich, request, retry }),
    [richPaths, intendedRichPaths, isIntendedRich, states, setRich, request, retry],
  );
}

/** The paths that show a rich diff now: chosen, and previewable. */
export function shownRichPaths(
  files: readonly DiffReviewFile[],
  contents: ReadonlyMap<string, DiffReviewContent>,
  richPaths: ReadonlySet<string>,
): ReadonlySet<string> {
  if (richPaths.size === 0) return richPaths;
  const shown = new Set<string>();
  for (const file of files) {
    if (richPaths.has(file.path) && canPreviewMarkdown(file, contents.get(file.path))) {
      shown.add(file.path);
    }
  }
  return shown;
}

let lastPreviewVersion = 1_000_000_000;

/** One review's preview rows by path; the review owns it, so closing the review frees them. */
export type MarkdownPreviewItemCache = Map<
  string,
  { collapsed: boolean; state: MarkdownPreviewState | undefined; item: CodeViewItem<unknown> }
>;

/**
 * Swap each rich file's diff item for an empty file whose line-0 row holds its
 * preview. An item keeps its identity until the file collapses or its preview changes.
 */
export function withMarkdownPreviewItems<A>(
  items: readonly CodeViewItem<A>[],
  shownRich: ReadonlySet<string>,
  states: ReadonlyMap<string, MarkdownPreviewState>,
  previewItemCache: MarkdownPreviewItemCache,
): CodeViewItem<A>[] {
  // A file that stopped showing its rich diff drops its row, and the document it held.
  for (const path of previewItemCache.keys()) {
    if (!shownRich.has(path)) previewItemCache.delete(path);
  }
  if (shownRich.size === 0) return items as CodeViewItem<A>[];
  return items.map((item) => {
    const path = pathFromReviewItemId(item.id);
    if (!shownRich.has(path)) return item;
    const collapsed = item.collapsed ?? false;
    const state = states.get(path);
    const cached = previewItemCache.get(path);
    if (cached && cached.collapsed === collapsed && cached.state === state) {
      return cached.item as CodeViewItem<A>;
    }
    lastPreviewVersion += 1;
    const preview = {
      id: item.id,
      type: "file",
      file: { name: path, contents: "", lang: "text", cacheKey: `${path}:markdown-preview` },
      annotations: [{ lineNumber: 0, metadata: MARKDOWN_PREVIEW_ANNOTATION }],
      collapsed,
      version: lastPreviewVersion,
    } as unknown as CodeViewItem<A>;
    previewItemCache.set(path, { collapsed, state, item: preview as CodeViewItem<unknown> });
    return preview;
  });
}

/**
 * What find searches for rich files: each rendered section once it is ready.
 * Until then the source lines stay searchable, so no match goes missing while a preview loads.
 */
export function withMarkdownPreviewSearchLines(
  lines: readonly ReviewSearchLine[],
  shownRich: ReadonlySet<string>,
  states: ReadonlyMap<string, MarkdownPreviewState>,
): ReviewSearchLine[] {
  if (shownRich.size === 0) return lines as ReviewSearchLine[];
  const result: ReviewSearchLine[] = [];
  for (const line of lines) {
    const path = pathFromReviewItemId(line.id);
    const state = shownRich.has(path) ? states.get(path) : undefined;
    if (state?.status !== "ready") {
      result.push(line);
      continue;
    }
    // A file's lines start with its paths; its sections take the place of its diff lines.
    if (line.target === "currentPath" || line.target === "previousPath") {
      result.push(line);
      if (line.target === "currentPath") {
        state.sections.forEach((section, index) => {
          if (section.text) {
            result.push({
              id: line.id,
              text: section.text,
              lineNumber: index + 1,
              target: "markdownPreview",
            });
          }
        });
      }
    }
  }
  return result;
}
