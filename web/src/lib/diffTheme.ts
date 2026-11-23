// The syntax theme diffs render with, and the colours every diff shares. A
// colour the theme doesn't define keeps the diff's own default.

import { SYNTAX_THEME_NAMES } from "./syntaxTheme";

/** The `theme` option for every diff. */
export const DIFF_SYNTAX_THEME = SYNTAX_THEME_NAMES;

/**
 * Palette colours every diff shares: line, gutter, and changed-word tints,
 * hover, the text selection, selected lines, and line numbers, all at the
 * strengths the palette's code colours were solved against.
 */
export const DIFF_PALETTE_CSS = `
  :host {
    --diffs-fg-number-override: var(--diff-number-text);
    --diffs-fg-number-addition-override: var(--diff-addition-text);
    --diffs-fg-number-deletion-override: var(--diff-deletion-text);
    --diffs-bg-addition-emphasis-override: rgb(from var(--diffs-addition-base) r g b / var(--diff-word-mix));
    --diffs-bg-deletion-emphasis-override: rgb(from var(--diffs-deletion-base) r g b / var(--diff-word-mix));
  }
  [data-line], [data-no-newline] {
    --mix-light: calc(100% - var(--diff-line-mix, 12%));
    --mix-dark: calc(100% - var(--diff-line-mix, 20%));
  }
  [data-gutter-buffer], [data-column-number] {
    --mix-light: calc(100% - var(--diff-gutter-mix, 9%));
    --mix-dark: calc(100% - var(--diff-gutter-mix, 15%));
  }
  [data-selected-line] { background-color: var(--code-selected-line, var(--sidebar-active)); }
  [data-column-number][data-selected-line], [data-gutter-buffer][data-selected-line] {
    color: var(--sidebar-active-foreground);
    box-shadow: inset -3px 0 var(--sidebar-active-foreground);
  }
  [data-line]::selection, [data-line] span::selection {
    background: var(--code-selection, var(--selection-background));
  }
  @media (pointer: fine) {
    [data-hovered] {
      --diffs-computed-hovered-line-bg: light-dark(
        color-mix(in lab, var(--diffs-computed-editor-active-line-bg) calc(100% - var(--diff-hover-mix, 3%)), var(--diffs-hover-mix-target)),
        color-mix(in lab, var(--diffs-computed-editor-active-line-bg) calc(100% - var(--diff-hover-mix, 9%)), var(--diffs-hover-mix-target))
      );
    }
  }
`;
