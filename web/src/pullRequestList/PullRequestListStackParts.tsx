// A stacked row's parts in the Pull Requests list: its tree lines and fold button before the
// title, and after it what's folded, why it isn't nested, or that its stack info may be stale.

import { ChevronRightIcon, CircleAlertIcon } from "lucide-react";

import { StackDepthPill } from "@/components/StackDepthPill";
import type { ListStackHint, ListStackPlacement } from "@/lib/pullRequestStacks/listNesting";
import type { StackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";

/** A stacked row as the list shows it. */
export interface ListStackRowView {
  placement: ListStackPlacement;
  /** For a row with rows nested under it: whether they're folded away. */
  collapsed: boolean;
}

export const STACK_INFO_OUT_OF_DATE = "Stack info may be out of date";

const parentName = (parent: StackPullRequestRef, external = false) =>
  external ? `${parent.repository}#${parent.number}` : `#${parent.number}`;

/** A hint's chip and its tooltip. */
export function stackHintCopy(hint: ListStackHint): { chip: string; detail: string } {
  switch (hint.kind) {
    case "absent":
      return {
        chip: "Part of a stack",
        detail: `Stacked on ${parentName(hint.parent)}, which isn’t shown here.`,
      };
    case "other-group":
      return {
        chip: "Part of a stack",
        detail: `Stacked on ${parentName(hint.parent)}, in ${hint.groupTitle}.`,
      };
    case "unpushed":
      return {
        chip: "Part of a stack",
        detail: `Stacked on ${hint.branch}, which isn’t pushed yet.`,
      };
    case "external":
      return { chip: "Part of a stack", detail: `Stacked on ${parentName(hint.parent, true)}.` };
    case "unconfirmed":
      return {
        chip: "Part of a stack",
        detail:
          hint.source === "github"
            ? `GitHub stacks this PR on ${parentName(hint.parent)}, but ${parentName(hint.parent)}’s own stack couldn’t be read here.`
            : `Stacked on ${parentName(hint.parent)}, but ${parentName(hint.parent)}’s description couldn’t be read here.`,
      };
    case "conflict":
      return { chip: "Stack info differs", detail: conflictDetail(hint.parent, hint.source) };
    case "native-gap":
      return { chip: "Part of a stack", detail: nativeGapDetail(hint.position, hint.size) };
  }
}

/** Which stack info disagrees about a row's parent, as its hint's source names it. */
function conflictDetail(
  parent: StackPullRequestRef,
  source: Extract<ListStackHint, { kind: "conflict" }>["source"],
): string {
  const name = parentName(parent);
  switch (source) {
    case "descriptions":
      return `This PR’s description stacks it on ${name}, but ${name}’s description doesn’t list it directly below.`;
    case "github":
      return `GitHub stacks this PR on ${name}, but GitHub’s read of ${name} doesn’t list it directly above.`;
    case "mixed":
      return `This PR’s description stacks it on ${name}, but GitHub’s stack doesn’t list it directly above ${name}.`;
  }
}

/** Where a PR sits in GitHub's stack, when the PR below it couldn't be read here. */
function nativeGapDetail(position: number | null, size: number | null): string {
  if (position === null) {
    return "Part of a GitHub stack whose other pull requests couldn’t be read here.";
  }
  const where = size === null ? `position ${position}` : `${position} of ${size}`;
  return `Stacked on another pull request in this stack (${where}).`;
}

/** The tree's lines before the title, then the fold button of a row with rows under it. */
export function StackTreeLead({
  stack,
  reference,
  onToggle,
}: {
  stack: ListStackRowView;
  reference: string;
  onToggle: () => void;
}) {
  const { placement, collapsed } = stack;
  return (
    <>
      {placement.connector !== null && (
        <span className="pr-list-stack-guides">
          {placement.guides
            .map((carries, level) => ({ carries, level }))
            .map((guide) => (
              <span key={guide.level} aria-hidden>
                {guide.carries ? "│" : ""}
              </span>
            ))}
          <span aria-hidden>{placement.connector === "tee" ? "├" : "└"}</span>
          {placement.deepLevel !== null && <StackDepthPill level={placement.deepLevel} />}
        </span>
      )}
      {/* A nested row with nothing under it runs its line through the fold button's place. */}
      {placement.descendants === 0 && placement.connector !== null && (
        <span aria-hidden className="pr-list-stack-toggle-space">
          ─
        </span>
      )}
      {placement.descendants > 0 && (
        <button
          type="button"
          // The list's keys fold it; the button is for the pointer.
          tabIndex={-1}
          className={`pr-list-stack-toggle${collapsed ? "" : " expanded"}`}
          aria-label={`${collapsed ? "Expand" : "Collapse"} the stack under ${reference}`}
          title={collapsed ? "Expand Stack" : "Collapse Stack"}
          // Focus stays where it was, so the list keeps its keys and its active row.
          onMouseDown={(event) => event.preventDefault()}
          onClick={(event) => {
            event.stopPropagation();
            onToggle();
          }}
        >
          <ChevronRightIcon aria-hidden />
        </button>
      )}
    </>
  );
}

/** After the title: how many rows are folded away, a hint, and stale stack info. */
export function StackRowNotes({ stack }: { stack: ListStackRowView }) {
  const { placement, collapsed } = stack;
  const hint = placement.hint && stackHintCopy(placement.hint);
  return (
    <>
      {collapsed && placement.descendants > 0 && (
        <span className="pr-list-stack-note">+{placement.descendants} in stack</span>
      )}
      {hint && (
        <span
          className="pr-list-stack-note"
          title={hint.detail}
          aria-label={`${hint.chip}. ${hint.detail}`}
        >
          {hint.chip}
        </span>
      )}
      {placement.tolerated && (
        <span
          className="pr-list-stack-stale"
          role="img"
          aria-label={STACK_INFO_OUT_OF_DATE}
          title={STACK_INFO_OUT_OF_DATE}
        >
          <CircleAlertIcon aria-hidden />
        </span>
      )}
    </>
  );
}
