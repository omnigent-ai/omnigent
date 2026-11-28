// How the new pull request page styles the sections the classic panel shares with it, and its
// sections' pickers that open from a press anywhere on the section.

import {
  createContext,
  type MouseEvent as ReactMouseEvent,
  useContext,
  useRef,
  useState,
} from "react";

/** The Summary tab's section titles: Labels, Reviewers, Checks, and Description. */
export const PULL_REQUEST_SECTION_TITLE_CLASS = "text-xs font-semibold text-muted-foreground";
/**
 * How a page shows its sections: the panel's small titles, a page's uppercase labels for
 * metadata such as Labels and Reviewers, or a page's headings for content such as Checks.
 */
export type PullRequestSectionStyle = "panel" | "overline" | "heading";
export const PullRequestSectionStyleContext = createContext<PullRequestSectionStyle>("panel");
const SECTION_TITLE_CLASSES: Record<PullRequestSectionStyle, string> = {
  panel: PULL_REQUEST_SECTION_TITLE_CLASS,
  overline: "text-[11px] font-bold tracking-[0.08em] text-muted-foreground uppercase",
  heading: "text-sm font-bold text-foreground",
};
/** The section style in effect, as the surrounding page set it. */
export function usePullRequestSectionStyle(): PullRequestSectionStyle {
  return useContext(PullRequestSectionStyleContext);
}
/** A section title's classes in the style in effect. */
export function usePullRequestSectionTitleClass(): string {
  return SECTION_TITLE_CLASSES[usePullRequestSectionStyle()];
}
/** A section picker's trigger, lit while the pointer is anywhere on its section. */
export const SECTION_PICKER_TRIGGER_CLASS =
  "text-muted-foreground group-hover/picker:bg-muted group-hover/picker:text-foreground";
/** What a press on a section leaves to the element pressed: its chips' own controls. */
const OWN_ACTION = "button, a, input, textarea, select, [role='button'], [role='menuitem']";
/**
 * A section whose picker also opens from a press anywhere on it, its title or empty space,
 * as GitHub's sidebar does. The picker's trigger stays the one keyboard stop; chips' own
 * controls keep their actions. Inert while `enabled` is false, e.g. read-only or waiting.
 */
export function useSectionPicker(enabled: boolean) {
  const [open, setOpen] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);
  const opensPicker = (target: EventTarget | null) =>
    enabled &&
    target instanceof Element &&
    !!sectionRef.current?.contains(target) &&
    !target.closest(OWN_ACTION);
  return {
    // A picker already open stays open while a change waits; its options say so.
    open,
    setOpen,
    sectionProps: {
      ref: sectionRef,
      onClick: (event: ReactMouseEvent<HTMLElement>) => {
        // Selecting a reviewer's login or a label's name to copy it isn't a press; a selection
        // elsewhere on the page doesn't stop one.
        const selection = window.getSelection();
        const selectingHere =
          !!selection?.toString() && !!sectionRef.current?.contains(selection.anchorNode);
        if (!opensPicker(event.target) || selectingHere) return;
        setOpen((current) => !current);
      },
      className: enabled ? "group/picker cursor-pointer" : undefined,
    },
    /** A press on the section toggles the picker, so it doesn't also count as outside it. */
    onPointerDownOutside: (event: { target: EventTarget | null; preventDefault: () => void }) => {
      if (opensPicker(event.target)) event.preventDefault();
    },
  };
}
