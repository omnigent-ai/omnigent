import { useLayoutEffect, useMemo, useRef, useState } from "react";

/**
 * Display candidates for a path, longest first: the path itself, then
 * `…/`-prefixed tails that drop one leading segment at a time down to the
 * final folder, e.g. `"/a/b/c"` → `["/a/b/c", "…/b/c", "…/c"]`.
 *
 * @param path Absolute host path; `\`-separated Windows paths keep `\`.
 */
export function leftTrimmedPathCandidates(path: string): string[] {
  const sep = !path.includes("/") && path.includes("\\") ? "\\" : "/";
  const segments = path.split(sep).filter(Boolean);
  const candidates = [path];
  for (let i = 1; i < segments.length; i += 1) {
    candidates.push(`…${sep}${segments.slice(i).join(sep)}`);
  }
  return candidates;
}

/**
 * Trim `path` from the left at segment boundaries until it fits the returned
 * ref's element, which must clip overflow (e.g. `truncate`). A final folder
 * that still overflows is left to the element's own end-ellipsis. The ref is
 * a callback so an element mounted later (e.g. when a tooltip opens) is
 * measured too; a new element or path restarts from the full path.
 */
export function useLeftTrimmedPath<T extends HTMLElement>(path: string) {
  const [el, ref] = useState<T | null>(null);
  const candidates = useMemo(() => leftTrimmedPathCandidates(path), [path]);
  const [trim, setTrim] = useState<{ el: T | null; path: string; index: number }>({
    el,
    path,
    index: 0,
  });
  const measured = useRef<{ el: T; path: string; width: number } | null>(null);
  const index = trim.el === el && trim.path === path ? trim.index : 0;
  useLayoutEffect(() => {
    if (!el) return;
    const width = el.clientWidth;
    if (
      measured.current?.el === el &&
      measured.current.path === path &&
      measured.current.width === width
    ) {
      return;
    }
    const probe = el.cloneNode(false) as T;
    probe.removeAttribute("id");
    probe.removeAttribute("data-testid");
    probe.style.position = "absolute";
    probe.style.visibility = "hidden";
    probe.style.width = "max-content";
    probe.style.maxWidth = "none";
    el.after(probe);
    let chosenIndex = 0;
    try {
      for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex += 1) {
        probe.textContent = candidates[candidateIndex];
        chosenIndex = candidateIndex;
        if (probe.scrollWidth <= width) break;
      }
    } finally {
      probe.remove();
    }
    measured.current = { el, path, width };
    setTrim({ el, path, index: chosenIndex });
  }, [el, path, candidates]);
  return { ref, text: candidates[index] };
}
