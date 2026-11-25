// Pieces the Summary tab's Reviewers and Labels sections share, so the two sit level
// side by side and their chips look and behave alike.

import {
  createContext,
  useContext,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/** A section title and its optional picker trigger, in a fixed-height row. */
export function PullRequestSectionHeader({
  id,
  title,
  action,
}: {
  id: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-1">
      <h3 id={id} className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {action}
    </div>
  );
}

/** Base classes of a reviewer or label chip; callers add the shape. */
export const PULL_REQUEST_CHIP_CLASS =
  "inline-flex max-w-full min-w-0 items-center gap-1.5 border border-border bg-muted/20 px-2 py-0.5 text-xs";

/** A chip's trailing icon button, e.g. remove. */
export function PullRequestChipAction({
  label,
  title,
  icon: Icon,
  onClick,
  disabled = false,
}: {
  label: string;
  title?: string;
  icon: LucideIcon;
  onClick: () => void;
  /** Shown but inert, with `title` saying why; still reachable by keyboard. */
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      aria-disabled={disabled || undefined}
      onClick={disabled ? undefined : onClick}
      className={cn(
        "shrink-0 rounded-full p-0.5 text-muted-foreground",
        disabled
          ? "cursor-default opacity-50"
          : "cursor-pointer hover:bg-muted hover:text-foreground",
      )}
    >
      <Icon aria-hidden className="size-3" />
    </button>
  );
}

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
        // Selecting a reviewer's login or a label's name to copy it isn't a press.
        if (!opensPicker(event.target) || !!window.getSelection()?.toString()) return;
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
