// The measured copies a selection bar renders for `useSelectionBarFit`: each part at each form,
// never interactive, out of the tab order and the accessibility tree, with no text of their own.

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";
import "./selectionBar.css";

/** The bar's inert row of copies, marked `[data-bar-sizers]` for the fit to find. */
export function SelectionBarSizers({ children }: { children: ReactNode }) {
  return (
    <span
      className="selection-bar-sizers"
      data-bar-sizers=""
      aria-hidden
      // React 18 drops a boolean `inert`; a string reaches the DOM.
      ref={(node) => node?.setAttribute("inert", "")}
    >
      <span className="selection-bar-sizer-row">{children}</span>
    </span>
  );
}

/** Text a copy takes the width of without adding it to the page's text. */
export function SizerText({ text, className }: { text: string; className?: string }) {
  return <span className={cn("selection-bar-sizer-text", className)} data-text={text} />;
}
