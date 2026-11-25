// The height each group of a rich diff's sections reserves while the browser skips it: the
// estimate for its sections at the current width, learned from sections already laid out, and
// once the group itself has been laid out, its real height.

import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { flushSync } from "react-dom";
import type { MarkdownDiffSection } from "./markdownDiff";
import { MARKDOWN_DIFF_SECTION_ATTRIBUTE } from "./markdownDiffSection";
import {
  heightModel,
  PROSE_CHARACTER_PER_FONT_PIXEL,
  rangeHeight,
  sectionShape,
  UNCALIBRATED,
  wantsSample,
  withSample,
  type HeightCalibration,
  type SectionReading,
  type SectionShape,
} from "./sectionHeightEstimate";

/** The sections' width until it's been measured: a typical review's. */
const DEFAULT_SECTIONS_WIDTH = 720;

/** Sections a laid-out group is read for calibration, at most, each time it resizes. */
const SAMPLES_PER_GROUP = 48;

/** The class each group's element has. */
export const SECTION_GROUP_CLASS = "markdown-diff-group";

function lineHeightOf(element: HTMLElement): number {
  const style = getComputedStyle(element);
  const lineHeight = parseFloat(style.lineHeight);
  return Number.isFinite(lineHeight) ? lineHeight : parseFloat(style.fontSize) * 1.2;
}

/** What `element`, a laid-out section of `shape`, shows of the preview's typography. */
function readSection(
  element: HTMLElement,
  shape: SectionShape,
  sectionsWidth: number,
): SectionReading {
  const block = element.firstElementChild;
  if (!(block instanceof HTMLElement)) return {};
  if (shape.kind === "code") {
    const code = block.querySelector<HTMLElement>("code") ?? block;
    return { typography: { codeLine: lineHeightOf(code) } };
  }
  if (shape.kind !== "prose" || block.localName !== "p") return {};
  const proseLine = lineHeightOf(block);
  const bounds = block.getBoundingClientRect();
  return {
    typography: {
      proseLine,
      proseCharacter: parseFloat(getComputedStyle(block).fontSize) * PROSE_CHARACTER_PER_FONT_PIXEL,
      proseInset: sectionsWidth - bounds.width,
    },
    paragraph: {
      width: bounds.width,
      lines: Math.round(bounds.height / proseLine),
      characters: shape.runs[0] ?? 0,
    },
  };
}

/**
 * Whether the browser lays `group`'s sections out now, rather than skipping them. The group's
 * own box always shows; only what's inside it is skipped.
 */
function laidOut(group: Element): boolean {
  return group.firstElementChild?.checkVisibility?.({ contentVisibilityAuto: true }) === true;
}

/**
 * `calibration`, taught by `group`'s sections if the browser has laid it out. A skipped group
 * is never read: reading it would lay it out.
 */
function calibrateFromGroup(
  group: HTMLElement,
  sections: readonly MarkdownDiffSection[],
  calibration: HeightCalibration,
  sectionsWidth: number,
): HeightCalibration {
  if (!laidOut(group)) return calibration;
  let next = calibration;
  let read = 0;
  for (const element of group.children) {
    if (read >= SAMPLES_PER_GROUP) break;
    const section = sections[Number(element.getAttribute(MARKDOWN_DIFF_SECTION_ATTRIBUTE)) - 1];
    if (!section || !(element instanceof HTMLElement)) continue;
    const shape = sectionShape(section);
    if (!wantsSample(next, shape)) continue;
    read += 1;
    next = withSample(
      next,
      shape,
      element.getBoundingClientRect().height,
      sectionsWidth,
      readSection(element, shape, sectionsWidth),
    );
  }
  return next;
}

/**
 * Makes the browser forget the height it remembers for each of `groups` it skips, so their
 * reserved estimate applies again: after a re-wrap, a height laid out at the old width is wrong.
 * `content-visibility: auto` keeps a remembered height whatever `contain-intrinsic-size` says;
 * one style pass with neither it nor an `auto` size drops it. The groups' nodes stay as they
 * are, and every group has its own styles back before this returns. Reads only the groups'
 * styles, and whether their first section is skipped; never a skipped group's geometry.
 */
export function forgetSkippedGroupSizes(groups: Iterable<HTMLElement>): number {
  const skipped: { group: HTMLElement; visibility: string; size: string }[] = [];
  for (const group of groups) {
    const first = group.firstElementChild;
    if (first?.checkVisibility?.({ contentVisibilityAuto: true }) !== false) continue;
    const { contentVisibility: visibility, containIntrinsicSize: size } = group.style;
    skipped.push({ group, visibility, size });
  }
  if (skipped.length === 0) return 0;
  try {
    for (const { group, size } of skipped) {
      group.style.contentVisibility = "hidden";
      group.style.containIntrinsicSize = size.replace(/^auto\s+/, "");
    }
    // One style pass over them all, while they're hidden.
    void getComputedStyle(skipped[0]!.group).containIntrinsicHeight;
  } finally {
    for (const { group, visibility, size } of skipped) {
      group.style.contentVisibility = visibility;
      group.style.containIntrinsicSize = size;
    }
  }
  return skipped.length;
}

/**
 * Each group's `contain-intrinsic-size`, by its index in `groups`: its estimate at the current width,
 * and once the browser has laid it out, its real height (`auto`). A width change makes the
 * browser forget the heights it remembers for groups it's skipping, laid out at the old width.
 */
export function useSectionGroupSizes(
  container: RefObject<HTMLElement | null>,
  sections: readonly MarkdownDiffSection[],
  groups: readonly { start: number; end: number }[],
  rendered: number,
): (index: number) => string | undefined {
  const [sectionsWidth, setSectionsWidth] = useState(DEFAULT_SECTIONS_WIDTH);
  // Learned from laid-out sections; kept across versions, since the typography is the same.
  const [calibration, setCalibration] = useState(UNCALIBRATED);
  const now = useRef({ width: sectionsWidth, calibration });
  now.current = { width: sectionsWidth, calibration };
  const observer = useRef<ResizeObserver | null>(null);
  // Each rendered group's size, until the width or the calibration changes.
  const sizes = useRef({ sections, entries: new Map<number, string>() });
  if (sizes.current.sections !== sections) sizes.current = { sections, entries: new Map() };

  useEffect(() => {
    const element = container.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    // The first width is where the reader starts: nothing has been laid out at another.
    let measuredOnce = false;
    const resizes = new ResizeObserver((entries) => {
      let calibrated = now.current.calibration;
      for (const entry of entries) {
        if (entry.target === element) {
          const measured = Math.round(entry.contentRect.width);
          if (measured <= 0 || measured === now.current.width) {
            measuredOnce ||= measured > 0;
            continue;
          }
          const resized = measuredOnce;
          measuredOnce = true;
          now.current = { ...now.current, width: measured };
          sizes.current.entries.clear();
          // Re-estimated in this frame, before the review puts the reader's place back: a
          // later change to groups above would move it again.
          flushSync(() => setSectionsWidth(measured));
          if (resized) {
            forgetSkippedGroupSizes(
              element.getElementsByClassName(SECTION_GROUP_CLASS) as HTMLCollectionOf<HTMLElement>,
            );
          }
        } else if (entry.target instanceof HTMLElement) {
          calibrated = calibrateFromGroup(entry.target, sections, calibrated, now.current.width);
        }
      }
      if (calibrated !== now.current.calibration) {
        now.current = { ...now.current, calibration: calibrated };
        sizes.current.entries.clear();
        setCalibration(calibrated);
      }
    });
    resizes.observe(element);
    observer.current = resizes;
    return () => {
      resizes.disconnect();
      observer.current = null;
    };
  }, [container, sections]);

  // Each group as it renders: its first real layout is what teaches the estimate.
  useEffect(() => {
    for (const group of container.current?.getElementsByClassName(SECTION_GROUP_CLASS) ?? []) {
      observer.current?.observe(group);
    }
  }, [container, rendered, sections]);

  const model = useMemo(() => heightModel(calibration), [calibration]);
  // Estimated as each group renders: a long document's first batch renders only its first groups.
  return useMemo(() => {
    const entries = sizes.current.entries;
    return (index: number) => {
      let size = entries.get(index);
      const group = groups[index];
      if (size === undefined && group) {
        size = `auto ${rangeHeight(sections, group.start, group.end, model, sectionsWidth)}px`;
        entries.set(index, size);
      }
      return size;
    };
  }, [groups, model, sections, sectionsWidth]);
}
