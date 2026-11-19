import { forwardRef, type ReactNode, type RefObject, useLayoutEffect } from "react";

import { findMentionTokens } from "@/lib/composerMentions";

/**
 * Pill look for an inline ``@`` token in the composer mirror. Background, ring
 * and color only: anything that changes glyph width (weight, padding) would
 * shift the mirror out of line with the textarea above it.
 */
export const MENTION_TOKEN_CLASS =
  "rounded-[4px] bg-brand-accent/10 text-brand-accent ring-2 ring-brand-accent/10";

/** ``text`` with each tagged token wrapped in a pill span. */
export function renderMentionTokens(text: string, tokens: readonly string[]): ReactNode {
  const ranges = findMentionTokens(text, tokens);
  if (ranges.length === 0) return text;
  const parts: ReactNode[] = [];
  let at = 0;
  for (const range of ranges) {
    parts.push(text.slice(at, range.start));
    parts.push(
      <span key={range.start} data-testid="composer-mention-token" className={MENTION_TOKEN_CLASS}>
        {range.token}
      </span>,
    );
    at = range.end;
  }
  parts.push(text.slice(at));
  return parts;
}

/**
 * Pin a mirror overlay to its textarea's box and scroll position while
 * ``active``. The input area also holds the textarea's line-box descent, which
 * would otherwise cut the mirror's scroll range short.
 */
export function useComposerMirrorAlignment(
  overlayRef: RefObject<HTMLDivElement | null>,
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  active: boolean,
) {
  useLayoutEffect(() => {
    const overlay = overlayRef.current;
    const textarea = textareaRef.current;
    if (!active || !overlay || !textarea) return;
    const align = () => {
      overlay.style.top = `${textarea.offsetTop}px`;
      overlay.style.height = `${textarea.offsetHeight}px`;
      overlay.scrollTop = textarea.scrollTop;
    };
    align();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(align);
    observer.observe(textarea);
    return () => observer.disconnect();
  }, [overlayRef, textareaRef, active]);
}

/** Classes that hand glyph painting to the mirror; the caret stays visible. */
export const MIRRORED_TEXTAREA_CLASS = "text-transparent caret-foreground";

/**
 * Mirror drawn behind a composer textarea, with the same typography so
 * wrapping matches. Mount it in ``ChatComposer``'s ``inputBackdrop`` slot.
 */
export const ComposerMirror = forwardRef<HTMLDivElement, { children: ReactNode }>(
  function ComposerMirror({ children }, ref) {
    return (
      <div
        ref={ref}
        aria-hidden
        data-testid="composer-highlight-overlay"
        className="composer-input-text pointer-events-none absolute inset-x-0 overflow-hidden whitespace-pre-wrap break-words px-3 text-ui text-foreground max-md:px-4"
      >
        {children}
      </div>
    );
  },
);
