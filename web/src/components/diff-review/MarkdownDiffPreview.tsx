// A Markdown file's rich diff: the rendered document with added and removed
// blocks marked, and changed words highlighted inside edited ones. Blocks come
// sanitized from the diff worker and render with the file viewer's elements.

import type { Element, RootContent } from "hast";
import { toJsxRuntime, type Components } from "hast-util-to-jsx-runtime";
import { memo, useEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef } from "react";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import {
  MARKDOWN_PREVIEW_COMPONENTS,
  MarkdownPreviewPathContext,
} from "@/shell/markdownPreviewComponents";
import { MARKDOWN_DIFF_STATIC_PROPERTY, type MarkdownDiffSection } from "./markdownDiff";
import { MARKDOWN_DIFF_SECTION_ATTRIBUTE } from "./markdownDiffSection";
import type { MarkdownPreviewState } from "./markdownDiffStore";
import { SECTION_GROUP_CLASS, useSectionGroupSizes } from "./useSectionGroupSizes";

const filePre = MARKDOWN_PREVIEW_COMPONENTS.pre as (
  props: ComponentPropsWithoutRef<"pre"> & { node?: Element },
) => React.ReactNode;

const DIFF_COMPONENTS = {
  ...MARKDOWN_PREVIEW_COMPONENTS,
  // Past the diagram limit, a Mermaid block shows its source instead of drawing.
  pre(props: ComponentPropsWithoutRef<"pre"> & { node?: Element }) {
    const { node, ...rest } = props;
    if (node?.properties[MARKDOWN_DIFF_STATIC_PROPERTY]) {
      delete (rest as Record<string, unknown>)["data-markdown-diff-static"];
      return <pre {...rest} />;
    }
    return filePre(rest);
  },
} as unknown as Partial<Components>;

const KIND_LABELS = { added: "Added", removed: "Removed" } as const;

const Section = memo(function Section({
  section,
  index,
}: {
  section: MarkdownDiffSection;
  index: number;
}) {
  const changed = section.kind !== "unchanged";
  return (
    <div
      className="markdown-diff-section"
      data-markdown-diff-kind={section.kind}
      {...{ [MARKDOWN_DIFF_SECTION_ATTRIBUTE]: index + 1 }}
      role={changed ? "group" : undefined}
      aria-label={changed ? KIND_LABELS[section.kind as "added" | "removed"] : undefined}
    >
      {section.oversized ? (
        <p className="markdown-diff-oversized">
          This block is too large to show in the rich diff. The source diff shows it.
        </p>
      ) : (
        toJsxRuntime(
          { type: "root", children: [section.block as RootContent] },
          {
            Fragment,
            jsx,
            jsxs,
            components: DIFF_COMPONENTS,
            passNode: true,
            ignoreInvalidStyle: true,
          },
        )
      )}
    </div>
  );
});

/** Rendered nodes per frame, so a long document never blocks the page for long. */
export const NODES_PER_FRAME = 3_000;

/**
 * Rendered nodes in the first frame, which also reads the worker's whole result and lays out
 * the review around the preview.
 */
export const NODES_IN_FIRST_FRAME = 1_000;

/**
 * Nodes per containment group. Off screen, a whole group skips layout and paint; on screen,
 * the browser lays all of it out, so this bounds that work, apart from React's per frame.
 */
export const NODES_PER_GROUP = 400;

/** The section indices `start` up to `end`. */
function sectionIndices(start: number, end: number): number[] {
  const indices: number[] = [];
  for (let index = start; index < end; index += 1) indices.push(index);
  return indices;
}

/** Each rendered document's version, so a section index read from one never names another's. */
const versions = new WeakMap<readonly MarkdownDiffSection[], string>();
let nextVersion = 0;
function versionOf(sections: readonly MarkdownDiffSection[]): string {
  let version = versions.get(sections);
  if (version === undefined) {
    nextVersion += 1;
    version = String(nextVersion);
    versions.set(sections, version);
  }
  return version;
}

/** A run of sections, `start` up to `end`, laid out or skipped together. */
export interface SectionGroup {
  start: number;
  end: number;
}

/**
 * The document's containment groups: each at least one section, then up to `budget` nodes. They
 * depend only on the sections, so a later render batch never moves an earlier boundary.
 */
export function sectionGroups(
  sections: readonly MarkdownDiffSection[],
  budget: number = NODES_PER_GROUP,
): SectionGroup[] {
  const groups: SectionGroup[] = [];
  for (let start = 0; start < sections.length;) {
    const end = nextBatchEnd(sections, start, budget);
    groups.push({ start, end });
    start = end;
  }
  return groups;
}

/** Where the batch that starts at `start` ends: at least one section, then up to `budget` nodes. */
export function nextBatchEnd(
  sections: readonly MarkdownDiffSection[],
  start: number,
  budget: number = NODES_PER_FRAME,
): number {
  let end = start;
  let nodes = 0;
  while (end < sections.length && (end === start || nodes + sections[end]!.nodes <= budget)) {
    nodes += sections[end]!.nodes;
    end += 1;
  }
  return end;
}

function Sections({ path, sections }: { path: string; sections: readonly MarkdownDiffSection[] }) {
  const [rendered, setRendered] = useState(() => ({
    sections,
    count: nextBatchEnd(sections, 0, NODES_IN_FIRST_FRAME),
  }));
  // A new version starts over from its first sections.
  const current = rendered.sections === sections;
  const count = current ? rendered.count : nextBatchEnd(sections, 0, NODES_IN_FIRST_FRAME);
  if (!current) setRendered({ sections, count });
  const groups = useMemo(() => sectionGroups(sections), [sections]);
  const container = useRef<HTMLDivElement>(null);
  const groupSize = useSectionGroupSizes(container, sections, groups, count);
  useEffect(() => {
    if (count >= sections.length) return;
    const frame = requestAnimationFrame(() =>
      setRendered({ sections, count: nextBatchEnd(sections, count) }),
    );
    return () => cancelAnimationFrame(frame);
  }, [count, sections]);
  if (sections.length === 0) {
    return <p className="markdown-diff-empty">This Markdown file is empty.</p>;
  }
  return (
    <div
      ref={container}
      data-markdown-diff-complete={count >= sections.length || undefined}
      data-markdown-diff-version={versionOf(sections)}
    >
      <MarkdownPreviewPathContext.Provider value={path}>
        {groups
          .filter((group) => group.start < count)
          .map((group, groupIndex) => (
            // A group keeps its element as later batches add its remaining sections.
            <div
              key={group.start}
              className={SECTION_GROUP_CLASS}
              style={{ containIntrinsicSize: groupSize(groupIndex) }}
            >
              {sectionIndices(group.start, Math.min(group.end, count)).map((index) => (
                // Sections are positional: a new version replaces the whole list.
                <Section key={index} section={sections[index]!} index={index} />
              ))}
            </div>
          ))}
      </MarkdownPreviewPathContext.Provider>
    </div>
  );
}

export interface MarkdownDiffPreviewProps {
  path: string;
  state: MarkdownPreviewState | undefined;
  /** Asks for the preview; runs once the preview is on screen. */
  onRequest: () => void;
  onRetry: () => void;
  onShowSource: () => void;
  /** The source diff has comments or conversations the preview doesn't show. */
  hasSourceComments: boolean;
  /**
   * The preview's width, first as laid out and then on each change of its size, with the
   * version of the document it shows then, or null for none.
   */
  onResize?: (width: number, version: string | null) => void;
}

export function MarkdownDiffPreview({
  path,
  state,
  onRequest,
  onRetry,
  onShowSource,
  hasSourceComments,
  onResize,
}: MarkdownDiffPreviewProps) {
  useEffect(() => {
    onRequest();
  }, [onRequest]);

  const root = useRef<HTMLElement>(null);
  const latestResize = useRef(onResize);
  latestResize.current = onResize;
  useEffect(() => {
    const element = root.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      // The border box, as the review reads the preview's width when it records the reader's place.
      const width = entry?.borderBoxSize?.[0]?.inlineSize ?? entry?.contentRect.width ?? 0;
      if (width <= 0) return;
      const version =
        element
          .querySelector("[data-markdown-diff-version]")
          ?.getAttribute("data-markdown-diff-version") ?? null;
      latestResize.current?.(width, version);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const showSourceButton = (
    <Button type="button" variant="outline" size="xs" onClick={onShowSource}>
      Show source diff
    </Button>
  );
  const shown =
    state?.status === "ready"
      ? state.sections
      : state?.status === "loading" || state?.status === "failed"
        ? state.previous
        : undefined;
  const outdated = state?.status !== "ready" && shown !== undefined;

  let status: React.ReactNode = null;
  if (!state || state.status === "loading") {
    status = (
      <div className="markdown-diff-status" role="status">
        <Spinner aria-hidden="true" />
        <span>{shown ? "Updating the rich diff…" : "Preparing the rich diff…"}</span>
      </div>
    );
  } else if (state.status === "failed") {
    status = (
      <div className="markdown-diff-status" role="alert">
        <span>{state.message}</span>
        <Button type="button" variant="outline" size="xs" onClick={onRetry}>
          Try again
        </Button>
        {showSourceButton}
      </div>
    );
  } else if (state.status === "unavailable") {
    status = (
      <div className="markdown-diff-status" role="status">
        <span>{state.message}</span>
        {showSourceButton}
      </div>
    );
  }

  return (
    <section
      ref={root}
      className="markdown-diff-preview"
      data-markdown-diff-path={path}
      aria-label={`Rich diff of ${path}`}
      aria-busy={state?.status === "loading" || undefined}
      data-testid="markdown-diff-preview"
    >
      {status}
      {hasSourceComments && state?.status === "ready" && (
        <div className="markdown-diff-status" role="note">
          <span>Comments on this file appear in the source diff.</span>
          {showSourceButton}
        </div>
      )}
      {shown && (
        <div
          className={cn(
            "markdown-preview prose prose-sm dark:prose-invert max-w-none",
            outdated && "markdown-diff-outdated",
          )}
          aria-hidden={outdated || undefined}
        >
          <Sections path={path} sections={shown} />
        </div>
      )}
    </section>
  );
}
