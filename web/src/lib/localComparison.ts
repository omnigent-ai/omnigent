// What Changes review says about the comparison it shows, in the reference
// design's terms: a base and a head ("origin/main … Working Copy"), the
// comparisons this session can switch to, and whether its changes can be shown.
// It names only what the data answers: the last commit only when the workspace
// is known to be Git, and nothing it can't serve in place of a chosen comparison.

import {
  type RevisionField,
  type RevisionSpec,
  sameRevisions,
  shortCommit,
  shownCommit,
} from "@/lib/customComparison";
import {
  CHECKING_REVISIONS,
  LAST_COMMIT_REVISIONS,
  QUICK_COMPARISONS,
  type QuickComparisonId,
  revisionsUnavailableReason,
  UNKNOWN_BASELINE_TITLE,
  type WorkingCopyBaseCommand,
} from "@/lib/workingCopyBaseCommands";
import {
  type BranchPointSupport,
  type EffectiveComparison,
  RECONNECTING,
  SHOWING_UNCOMMITTED,
} from "@/lib/workingCopyComparison";
import type { WorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";

/** The head every local comparison has today: the files as they are now. */
export const WORKING_COPY = "Working Copy";

export { CHECKING_REVISIONS, UNKNOWN_BASELINE_TITLE };
/** The trigger's name for a typed comparison that isn't one of the quick choices. */
export const CUSTOM_COMPARISON = "Custom";
export const COMPARISON_UNAVAILABLE = "Comparison Unavailable";
export const NO_ACCESS_TITLE = "Changes Unavailable";
export const NO_ACCESS_MESSAGE = "You don't have access to this session's files.";
/** The head choice's name while the workspace isn't known to be Git: what it is compared with is unknown. */
export const UNKNOWN_BASE = "Unknown base";
/** A fallback note's opening when the host's changes aren't known to be against the last commit. */
export const SHOWING_HOST_CHANGES = "Showing the changes the host reports";

/** A comparison the popover offers in one step, ahead of the manual Base and Head. */
export interface QuickComparison {
  id: QuickComparisonId;
  /** "Uncommitted changes", "All branch changes" or "Last commit". */
  label: string;
  description: string;
  /** It's the comparison on screen; for display only, apart from the saved base. */
  active: boolean;
  /** Why it can't be chosen here; null when it can. */
  unavailableReason: string | null;
  /** Runs the shared command, even for the active one, so the choice is saved as made. */
  choose: () => void;
}

/** A comparison the base selector offers. */
export interface LocalComparisonPreset {
  id: "head" | "branch-point";
  /** As the reference's base field shows it, such as `HEAD` or `origin/main (merge base)`. */
  label: string;
  selected: boolean;
  /** Why it can't be chosen here; null when it can. */
  unavailableReason: string | null;
  choose: () => void;
}

/** Whether the session's changes can be shown under the comparison. */
export type LocalComparisonState =
  | {
      kind: "ready";
      /** Set while the changes on screen can't be refreshed: they are kept, not current. */
      status: { kind: "reconnecting" | "stale"; message: string } | null;
    }
  | { kind: "pending"; message: string }
  | { kind: "unavailable"; title: string; message: string; canRetry: boolean }
  /** The viewer may not read this session's files; nothing retained is shown. */
  | { kind: "no-access"; title: string; message: string };

/** Automatic, or "Show uncommitted changes", showed the last commit instead of the branch point. */
export interface LocalComparisonFallback {
  message: string;
  /** Trying the branch point again may help. */
  canRetry: boolean;
  /** The person chose uncommitted changes for this session; comparing again ends that. */
  override: boolean;
  /** Tries the branch point again: rechecks the route and restarts its wait. */
  retry: () => void;
}

/** Typing a base and head for this session. */
export interface LocalComparisonRevisions {
  /** Base and Head can be typed here; otherwise `revisionsNote` says why. */
  editable: boolean;
  /** The typed comparison shown; null while the session shows its saved base. */
  custom: RevisionSpec | null;
  /** The field to fix, from the desktop app's answer to the typed comparison. */
  fieldError: { field: RevisionField; message: string } | null;
  /** Compares the typed base and head for this session, in place of its saved base. */
  compare: (spec: RevisionSpec) => void;
}

export interface LocalComparisonView {
  /**
   * The toolbar's trigger. `name` is the comparison in plain words, or null when
   * the host didn't say what it compares with; then it shows base … head, and a
   * null base. The refs are in `title`.
   */
  trigger: {
    name: string | null;
    base: string | null;
    head: string;
    title: string;
    invalid: boolean;
  };
  quickChoices: QuickComparison[];
  presets: LocalComparisonPreset[];
  /** Back to Automatic; disabled while it's already the choice. */
  reset: { disabled: boolean; run: () => void };
  revisions: LocalComparisonRevisions;
  /**
   * What the person chose to compare: the saved base, a typed base and head, or the temporary
   * uncommitted changes. It changes when they choose again, never as an answer arrives.
   */
  choiceKey: string;
  /** Why the base and head can't be typed here; null when they can. */
  revisionsNote: string | null;
  fallback: LocalComparisonFallback | null;
  state: LocalComparisonState;
  refresh: () => void;
  /**
   * Loads the source on screen again: the runner's list for HEAD, the desktop app
   * for the branch point. A fallback's own `retry` tries the branch point instead.
   */
  retry: () => void;
  /** Shows uncommitted changes for this session only, a temporary override, not Automatic. */
  showLastCommit: () => void;
  /** Ends "Show uncommitted changes" and compares with the saved base again. */
  compareAgain: () => void;
}

/** How the view reaches the comparison's sources. */
export interface LocalComparisonActions {
  refetchHead: () => void;
  retryBranchPoint: () => void;
  showLastCommit: () => void;
  compareAgain: () => void;
  compareRevisions: (spec: RevisionSpec) => void;
}

export interface LocalComparisonInput {
  comparison: EffectiveComparison;
  /** Non-null when the workspace is known to be a Git worktree, so its base is HEAD. */
  headIsKnown: boolean;
  /** Whether a typed base and head can be compared for this session. */
  revisionSupport: BranchPointSupport;
  /** The base and head the person typed for this session, if any. */
  custom: RevisionSpec | null;
  intent: WorkingCopyBaseIntent;
  /** The shared base commands, so the selector, menus and ⌘K can't drift. */
  commands: readonly WorkingCopyBaseCommand[];
  /** The viewer may read this session's files. */
  permitted: boolean;
  /** New reads can start now; without it, what's on screen is kept but not current. */
  readable: boolean;
  actions: LocalComparisonActions;
}

const DEFAULT_BRANCH = "Default branch";

const QUICK_DESCRIPTIONS: Record<QuickComparisonId, (ref: string) => string> = {
  "compare-head": () => "Your working copy against the last commit",
  "compare-branch-point": (ref) =>
    `Everything since this branch left ${ref || "the default branch"}`,
  "compare-last-commit": () => "What the last commit changed",
};

/**
 * The quick choice the comparison on screen is, or the one asked for while it's
 * pending or unavailable. "custom" is another typed comparison; null means the
 * host didn't say what it compares with.
 */
function requestedChoice(
  comparison: EffectiveComparison,
  headIsKnown: boolean,
  custom: RevisionSpec | null,
): QuickComparisonId | "custom" | null {
  if (custom)
    return sameRevisions(custom, LAST_COMMIT_REVISIONS) ? "compare-last-commit" : "custom";
  if (comparison.base !== "head") return "compare-branch-point";
  return headIsKnown ? "compare-head" : null;
}

function choiceName(
  commands: readonly WorkingCopyBaseCommand[],
  choice: QuickComparisonId | "custom" | null,
): string | null {
  if (choice === null) return null;
  if (choice === "custom") return CUSTOM_COMPARISON;
  return command(commands, choice)?.name ?? null;
}

function command(
  commands: readonly WorkingCopyBaseCommand[],
  id: WorkingCopyBaseCommand["id"],
): WorkingCopyBaseCommand | undefined {
  return commands.find((candidate) => candidate.id === id);
}

function refsFor(
  comparison: EffectiveComparison,
  headIsKnown: boolean,
  custom: RevisionSpec | null,
): Omit<LocalComparisonView["trigger"], "name"> {
  const compared = (base: string) => `${base} compared with ${WORKING_COPY}`;
  if (custom) {
    // Always the typed names: nothing else is shown under them.
    const head = custom.head || WORKING_COPY;
    const settled = comparison.base === "branch-point";
    const commit = shownCommit(custom);
    return {
      base: custom.base,
      head,
      title:
        settled || comparison.base === "head"
          ? commit
            ? `What commit ${shortCommit(commit)} changed`
            : `${custom.base} compared with ${head}`
          : comparison.message,
      invalid: comparison.base === "unavailable",
    };
  }
  switch (comparison.base) {
    case "head":
      return headIsKnown
        ? { base: "HEAD", head: WORKING_COPY, title: compared("HEAD"), invalid: false }
        : { base: null, head: WORKING_COPY, title: UNKNOWN_BASELINE_TITLE, invalid: false };
    case "branch-point": {
      const ref = comparison.comparison.ref || DEFAULT_BRANCH;
      return { base: ref, head: WORKING_COPY, title: compared(ref), invalid: false };
    }
    case "pending":
      return {
        base: DEFAULT_BRANCH,
        head: WORKING_COPY,
        title: comparison.message,
        invalid: false,
      };
    case "unavailable":
      return { base: DEFAULT_BRANCH, head: WORKING_COPY, title: comparison.message, invalid: true };
  }
}

function stateFor(
  comparison: EffectiveComparison,
  permitted: boolean,
  readable: boolean,
): LocalComparisonState {
  if (!permitted) return { kind: "no-access", title: NO_ACCESS_TITLE, message: NO_ACCESS_MESSAGE };
  switch (comparison.base) {
    case "head":
      // Last-commit changes the runner listed before the workspace went out of reach.
      return {
        kind: "ready",
        status: readable ? null : { kind: "reconnecting", message: RECONNECTING },
      };
    case "branch-point":
      return { kind: "ready", status: comparison.status };
    case "pending":
      return { kind: "pending", message: comparison.message };
    case "unavailable":
      return {
        kind: "unavailable",
        title: COMPARISON_UNAVAILABLE,
        message: comparison.message,
        canRetry: comparison.retry,
      };
  }
}

/** A fallback note that doesn't claim the host's changes are against the last commit. */
function neutralNote(note: string): string {
  return note.startsWith(SHOWING_UNCOMMITTED)
    ? SHOWING_HOST_CHANGES + note.slice(SHOWING_UNCOMMITTED.length)
    : note;
}

function quickChoicesFor(
  commands: readonly WorkingCopyBaseCommand[],
  shown: QuickComparisonId | "custom" | null,
  comparison: EffectiveComparison,
  ref: string,
): QuickComparison[] {
  // Only a comparison on screen is marked, not one still pending or unavailable.
  const settled = comparison.base === "head" || comparison.base === "branch-point";
  return QUICK_COMPARISONS.flatMap((id) => {
    const shared = command(commands, id);
    if (!shared) return [];
    return [
      {
        id,
        label: shared.name,
        description: QUICK_DESCRIPTIONS[id](ref),
        active: settled && shown === id,
        unavailableReason: shared.unavailableReason,
        choose: shared.run,
      },
    ];
  });
}

/** Changes review's view of the comparison a session's changes are shown under. */
export function localComparisonView(input: LocalComparisonInput): LocalComparisonView {
  const { comparison, headIsKnown, intent, commands, permitted, readable, actions } = input;
  const { custom, revisionSupport } = input;
  // Only the branch point's own listing names the default branch it was taken from.
  const ref =
    comparison.base === "branch-point" && !comparison.comparison.revisions
      ? comparison.comparison.ref
      : "";
  const head = command(commands, "compare-head");
  const branchPoint = command(commands, "compare-branch-point");
  const automatic = command(commands, "compare-automatic");
  const refresh = command(commands, "refresh-changes");
  const showsBranchPoint = comparison.base !== "head";

  const presets: LocalComparisonPreset[] = [];
  if (branchPoint) {
    presets.push({
      id: "branch-point",
      label: `${ref || DEFAULT_BRANCH} (merge base)`,
      selected: !custom && showsBranchPoint,
      unavailableReason: branchPoint.unavailableReason,
      choose: branchPoint.run,
    });
  }
  if (head) {
    presets.push({
      id: "head",
      // Without a known Git worktree, nothing says the host compares with HEAD, so it can't be picked as such.
      label: headIsKnown ? "HEAD" : UNKNOWN_BASE,
      selected: !custom && !showsBranchPoint,
      unavailableReason: headIsKnown ? head.unavailableReason : UNKNOWN_BASELINE_TITLE,
      choose: headIsKnown ? head.run : () => {},
    });
  }

  const fallback =
    comparison.base === "head" && comparison.note
      ? {
          message: headIsKnown ? comparison.note : neutralNote(comparison.note),
          canRetry: comparison.retry,
          override: comparison.override,
          retry: actions.retryBranchPoint,
        }
      : null;
  // The default is applied only when Automatic is chosen and no session override replaces it.
  const defaultApplied =
    !custom && intent === "automatic" && !(comparison.base === "head" && comparison.override);
  const fieldError =
    custom && comparison.base === "unavailable" && comparison.field
      ? { field: comparison.field, message: comparison.message }
      : null;

  const requested = requestedChoice(comparison, headIsKnown, custom);
  const commit = shownCommit(custom);
  return {
    trigger: {
      name: commit ? `Commit ${shortCommit(commit)}` : choiceName(commands, requested),
      ...refsFor(comparison, headIsKnown, custom),
    },
    quickChoices: quickChoicesFor(commands, requested, comparison, ref),
    presets,
    reset: { disabled: defaultApplied, run: automatic?.run ?? (() => {}) },
    revisions: {
      editable: revisionSupport.kind === "available",
      custom,
      fieldError,
      compare: actions.compareRevisions,
    },
    choiceKey: JSON.stringify([
      intent,
      custom?.base ?? null,
      custom?.head ?? null,
      comparison.base === "head" && comparison.override,
    ]),
    revisionsNote: revisionsUnavailableReason(revisionSupport),
    fallback,
    state: stateFor(comparison, permitted, readable),
    refresh: refresh?.run ?? (() => {}),
    retry: comparison.base === "head" ? actions.refetchHead : actions.retryBranchPoint,
    showLastCommit: actions.showLastCommit,
    compareAgain: actions.compareAgain,
  };
}

/**
 * The view with `onChoose` run before each comparison it offers is chosen, so a surface showing
 * something else can show the result. Refreshing and retrying aren't choices.
 */
export function onComparisonChosen(
  view: LocalComparisonView,
  onChoose: () => void,
): LocalComparisonView {
  const chosen =
    <Args extends unknown[]>(run: (...args: Args) => void) =>
    (...args: Args) => {
      onChoose();
      run(...args);
    };
  return {
    ...view,
    quickChoices: view.quickChoices.map((choice) => ({ ...choice, choose: chosen(choice.choose) })),
    presets: view.presets.map((preset) => ({ ...preset, choose: chosen(preset.choose) })),
    reset: { ...view.reset, run: chosen(view.reset.run) },
    revisions: { ...view.revisions, compare: chosen(view.revisions.compare) },
    showLastCommit: chosen(view.showLastCommit),
    compareAgain: chosen(view.compareAgain),
  };
}
