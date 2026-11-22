// The diff review's view commands. The toolbar and ⌘K both read these
// definitions, so a control and its command share a label, state, and handler.

import {
  ArrowDownWideNarrowIcon,
  ChartBarIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  Columns2Icon,
  FilterIcon,
  FlaskConicalIcon,
  FlaskConicalOffIcon,
  ListTreeIcon,
  type LucideIcon,
  PilcrowIcon,
  Rows2Icon,
  SearchIcon,
  UserCheckIcon,
  WrapTextIcon,
} from "lucide-react";
import { AVAILABLE, unavailableBecause, type PaletteCommand } from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";
import {
  nextTestFileView,
  testFileViewLabel,
  type DiffFileOrder,
  type DiffTestFileView,
} from "./diffReviewModel";

export type DiffReviewCommandId =
  | "toggle-tree"
  | "search-files"
  | "filter-files"
  | "show-language-breakdown"
  | "show-hidden-matches"
  | "toggle-generated-files"
  | "toggle-owned-files"
  | "filter-languages"
  | "cycle-test-files"
  | "toggle-layout"
  | "toggle-whitespace"
  | "toggle-wrap"
  | "sort-files"
  | "toggle-collapse-all";

export interface DiffReviewCommand {
  id: DiffReviewCommandId;
  /** What running it does, for the control's name, tooltip, and the palette row. */
  label: string;
  icon: LucideIcon;
  keywords: string[];
  /** Whether the setting it toggles is on; absent for one-shot actions. */
  pressed?: boolean;
  /** Why it can't run right now; the toolbar disables it and ⌘K greys it out with this. */
  unavailableReason?: string;
  /** Whether running it moves focus, such as into the file search. */
  movesFocus?: boolean;
  /** For a view setting: how ⌘K shows it, one row with a page of every value. */
  setting?: DiffReviewSetting;
  run: () => void;
}

/** A view setting's ⌘K form, sharing the toolbar control's state and handler. */
export interface DiffReviewSetting {
  label: string;
  current: string;
  choices: readonly { value: string; label: string }[];
  choose: (value: string) => void;
}

/** The view settings as ⌘K names them, in the review and the file viewer alike. */
export const DIFF_LAYOUT_SETTING_LABEL = "Diff layout";
export const WRAP_LINES_SETTING_LABEL = "Wrap long lines";
export const FILE_ORDER_SETTING_LABEL = "Sort changed files";
/** The file orders as the options popover and ⌘K name them. */
export const FILE_ORDER_CHOICES: readonly { value: DiffFileOrder; label: string }[] = [
  { value: "path", label: "By path" },
  { value: "size", label: "By size, largest first" },
];
export const HIDE_WHITESPACE_SETTING_LABEL = "Hide whitespace changes";
export const TEST_FILES_SETTING_LABEL = "Test files";
export const GENERATED_FILES_SETTING_LABEL = "Generated files";
export const OWNED_FILES_SETTING_LABEL = "Files you own";
export const LANGUAGES_SETTING_LABEL = "Languages";
/** Find in a diff review, its toolbar button and its bar alike. */
export const FIND_IN_CHANGES_LABEL = "Find in changes";
/** Why the language breakdown can't open: no file is listed, as when filters hide them all. */
export const NO_LISTED_FILES = "No changed files are listed.";
const ALL_LANGUAGES = "all";
const SOME_LANGUAGES = "some";

const ON_OFF = [
  { value: "on", label: "On" },
  { value: "off", label: "Off" },
] as const;

function onOffSetting(label: string, on: boolean, write: (on: boolean) => void): DiffReviewSetting {
  return {
    label,
    current: on ? "on" : "off",
    choices: ON_OFF,
    choose: (value) => write(value === "on"),
  };
}

const TEST_FILE_VIEW_NAMES: Record<DiffTestFileView, string> = {
  all: "All files",
  skipTests: "Skip test files",
  onlyTests: "Only test files",
};

/** What the review's commands read and change; a missing setter withholds that command. */
export interface DiffReviewCommandState {
  treeShown: boolean;
  toggleTree: () => void;
  searchFiles: () => void;
  filtersActive: boolean;
  openFilters: () => void;
  /**
   * Find's matches in the files the filters leave out, and Show, which clears those filters;
   * absent where the review has no find.
   */
  hiddenMatches?: { findOpen: boolean; count: number; show: () => void };
  /** How many files the review lists now, after its filters and search. */
  listedFileCount: number;
  /** Opens the toolbar's breakdown of the listed files' changed lines by language. */
  openLanguageBreakdown: () => void;
  /** Offers showing or hiding generated files as a row of its own; absent leaves it to the filter menu. */
  generatedFiles?: { shown: boolean; count: number; setShown: (shown: boolean) => void };
  /** Offers showing only the files the reader owns; absent while they own none. */
  ownedFiles?: { only: boolean; count: number; setOnly: (only: boolean) => void };
  /** Offers narrowing to one language as a row of its own; absent leaves it to the filter menu. */
  languages?: {
    counts: readonly { language: string; count: number }[];
    /** The languages shown; null shows every one. */
    selected: readonly string[] | null;
    /** Shows only `language`, or every language for null. */
    showOnly: (language: string | null) => void;
  };
  testFileView: DiffTestFileView;
  setTestFileView: (view: DiffTestFileView) => void;
  layout: "unified" | "split";
  /** Absent when the source doesn't offer the choice or the review is too narrow to split. */
  setLayout?: (layout: "unified" | "split") => void;
  ignoreWhitespace: boolean;
  setIgnoreWhitespace?: (ignore: boolean) => void;
  wrapLines: boolean;
  setWrapLines?: (wrap: boolean) => void;
  fileOrder: DiffFileOrder;
  setFileOrder: (order: DiffFileOrder) => void;
  allCollapsed: boolean;
  canCollapse: boolean;
  toggleAllFiles: () => void;
}

/** Shows only the reader's files, or all of them: the palette, filter menu, and file badges share it. */
export function ownedFilesCommand(
  ownedFiles: NonNullable<DiffReviewCommandState["ownedFiles"]>,
): DiffReviewCommand {
  return {
    id: "toggle-owned-files",
    label: ownedFiles.only ? "Show all changed files" : "Show only files you own",
    icon: UserCheckIcon,
    keywords: ["owned", "owners", "mine", "yours", "filter", "approve"],
    pressed: ownedFiles.only,
    setting: {
      label: OWNED_FILES_SETTING_LABEL,
      current: ownedFiles.only ? "only" : "all",
      choices: [
        { value: "all", label: "Show all files" },
        { value: "only", label: `Show only files you own (${ownedFiles.count})` },
      ],
      choose: (value) => ownedFiles.setOnly(value === "only"),
    },
    run: () => ownedFiles.setOnly(!ownedFiles.only),
  };
}

/** The review's commands, in toolbar order. */
export function diffReviewCommands(state: DiffReviewCommandState): DiffReviewCommand[] {
  const commands: DiffReviewCommand[] = [
    {
      id: "toggle-tree",
      label: state.treeShown ? "Hide changed files" : "Show changed files",
      icon: ListTreeIcon,
      keywords: ["tree", "sidebar", "files"],
      pressed: state.treeShown,
      run: state.toggleTree,
    },
    {
      id: "search-files",
      label: "Search changed files",
      icon: SearchIcon,
      keywords: ["find", "file", "name", "tree"],
      movesFocus: true,
      run: state.searchFiles,
    },
    {
      id: "filter-files",
      label: "Filter changed files",
      icon: FilterIcon,
      keywords: ["language", "generated", "tests"],
      pressed: state.filtersActive,
      movesFocus: true,
      run: state.openFilters,
    },
    {
      id: "show-language-breakdown",
      label: "Show changes by language",
      icon: ChartBarIcon,
      keywords: ["language", "lines", "added", "removed", "stats", "breakdown"],
      unavailableReason: state.listedFileCount > 0 ? undefined : NO_LISTED_FILES,
      movesFocus: true,
      run: state.openLanguageBreakdown,
    },
  ];
  const { generatedFiles, languages, ownedFiles, hiddenMatches } = state;
  if (hiddenMatches) {
    commands.push({
      id: "show-hidden-matches",
      label: "Show hidden matches",
      icon: SearchIcon,
      keywords: ["find", "hidden", "filtered", "matches", "show all"],
      unavailableReason: !hiddenMatches.findOpen
        ? "Find isn't open in this review."
        : hiddenMatches.count === 0
          ? "No find matches are in hidden files."
          : undefined,
      movesFocus: true,
      run: hiddenMatches.show,
    });
  }
  if (ownedFiles) commands.push(ownedFilesCommand(ownedFiles));
  if (generatedFiles) {
    commands.push({
      id: "toggle-generated-files",
      label: generatedFiles.shown ? "Hide generated files" : "Show generated files",
      icon: FilterIcon,
      keywords: ["generated", "filter", "lockfile", "vendored"],
      pressed: !generatedFiles.shown,
      unavailableReason:
        generatedFiles.count > 0 ? undefined : "None of the changed files are generated.",
      setting: {
        label: GENERATED_FILES_SETTING_LABEL,
        current: generatedFiles.shown ? "shown" : "hidden",
        choices: [
          { value: "shown", label: "Show" },
          { value: "hidden", label: "Hide" },
        ],
        choose: (value) => generatedFiles.setShown(value === "shown"),
      },
      run: () => generatedFiles.setShown(!generatedFiles.shown),
    });
  }
  if (languages && languages.counts.length > 1) {
    const { selected } = languages;
    const only = selected?.length === 1 ? selected[0] : null;
    // None, or several but not all: a value of its own, so choosing All languages still applies.
    const subset = selected !== null && only === null;
    const current = selected === null ? ALL_LANGUAGES : (only ?? SOME_LANGUAGES);
    commands.push({
      id: "filter-languages",
      label: "Show one language",
      icon: FilterIcon,
      keywords: ["language", "filter"],
      pressed: languages.selected !== null,
      setting: {
        label: LANGUAGES_SETTING_LABEL,
        current,
        choices: [
          { value: ALL_LANGUAGES, label: "All languages" },
          ...(subset
            ? [
                {
                  value: SOME_LANGUAGES,
                  label:
                    selected.length === 0
                      ? "No languages"
                      : `${selected.length} of ${languages.counts.length} languages`,
                },
              ]
            : []),
          ...languages.counts.map(({ language, count }) => ({
            value: language,
            label: `${language} (${count})`,
          })),
        ],
        choose: (value) => {
          if (value === SOME_LANGUAGES) return;
          languages.showOnly(value === ALL_LANGUAGES ? null : value);
        },
      },
      run: () => languages.showOnly(null),
    });
  }
  const nextTestView = nextTestFileView(state.testFileView);
  commands.push({
    id: "cycle-test-files",
    label: testFileViewLabel(nextTestView),
    icon: state.testFileView === "skipTests" ? FlaskConicalOffIcon : FlaskConicalIcon,
    keywords: ["tests", "filter"],
    pressed: state.testFileView !== "all",
    setting: {
      label: TEST_FILES_SETTING_LABEL,
      current: state.testFileView,
      choices: (["all", "skipTests", "onlyTests"] as const).map((view) => ({
        value: view,
        label: TEST_FILE_VIEW_NAMES[view],
      })),
      choose: (value) => {
        if (value === "all" || value === "skipTests" || value === "onlyTests") {
          state.setTestFileView(value);
        }
      },
    },
    run: () => state.setTestFileView(nextTestView),
  });
  const { setLayout, setIgnoreWhitespace, setWrapLines } = state;
  if (setLayout) {
    const split = state.layout === "split";
    commands.push({
      id: "toggle-layout",
      label: split ? "Switch to unified view" : "Switch to split view",
      icon: split ? Rows2Icon : Columns2Icon,
      keywords: ["layout", "side by side", "inline", "split", "unified"],
      pressed: split,
      setting: {
        label: DIFF_LAYOUT_SETTING_LABEL,
        current: state.layout,
        choices: [
          { value: "split", label: "Side by side" },
          { value: "unified", label: "Inline" },
        ],
        choose: (value) => {
          if (value === "split" || value === "unified") setLayout(value);
        },
      },
      run: () => setLayout(split ? "unified" : "split"),
    });
  }
  if (setIgnoreWhitespace) {
    const ignoring = state.ignoreWhitespace;
    commands.push({
      id: "toggle-whitespace",
      label: ignoring ? "Show whitespace changes" : "Hide whitespace changes",
      icon: PilcrowIcon,
      keywords: ["whitespace", "indentation", "spaces"],
      pressed: ignoring,
      setting: onOffSetting(HIDE_WHITESPACE_SETTING_LABEL, ignoring, setIgnoreWhitespace),
      run: () => setIgnoreWhitespace(!ignoring),
    });
  }
  if (setWrapLines) {
    const wrapping = state.wrapLines;
    commands.push({
      id: "toggle-wrap",
      label: wrapping ? "Don’t wrap lines" : "Wrap lines",
      icon: WrapTextIcon,
      keywords: ["wrap", "soft wrap", "long lines"],
      pressed: wrapping,
      setting: onOffSetting(WRAP_LINES_SETTING_LABEL, wrapping, setWrapLines),
      run: () => setWrapLines(!wrapping),
    });
  }
  const { fileOrder, setFileOrder } = state;
  const bySize = fileOrder === "size";
  commands.push({
    id: "sort-files",
    label: bySize ? "Sort files by path" : "Sort files by size",
    icon: ArrowDownWideNarrowIcon,
    keywords: ["sort", "order", "size", "largest", "biggest", "lines", "path"],
    pressed: bySize,
    setting: {
      label: FILE_ORDER_SETTING_LABEL,
      current: fileOrder,
      choices: FILE_ORDER_CHOICES,
      choose: (value) => {
        if (value === "path" || value === "size") setFileOrder(value);
      },
    },
    run: () => setFileOrder(bySize ? "path" : "size"),
  });
  commands.push({
    id: "toggle-collapse-all",
    label: state.allCollapsed ? "Expand all files" : "Collapse all files",
    icon: state.allCollapsed ? ChevronsUpDownIcon : ChevronsDownUpIcon,
    keywords: ["collapse", "expand", "fold"],
    unavailableReason: state.canCollapse ? undefined : "No listed file has a diff to collapse.",
    run: state.toggleAllFiles,
  });
  return commands;
}

/** The review's commands as palette rows: its view settings as setting rows, the rest as actions. */
export function diffReviewPaletteCommands(
  scope: string,
  commands: readonly DiffReviewCommand[],
): PaletteCommand[] {
  return commands.map((command) => {
    const keywords = ["diff", "review", "changes", ...command.keywords];
    const availability =
      command.unavailableReason === undefined
        ? AVAILABLE
        : unavailableBecause(command.unavailableReason);
    const { setting } = command;
    if (setting) {
      return settingCommand({
        id: `diff-review-${command.id}`,
        owner: scope,
        label: setting.label,
        icon: command.icon,
        keywords,
        availability,
        current: setting.current,
        choices: setting.choices,
        choose: setting.choose,
      });
    }
    return {
      id: `${scope}-diff-review-${command.id}`,
      label: command.label,
      icon: command.icon,
      keywords,
      availability,
      movesFocus: command.movesFocus,
      run: command.run,
    };
  });
}
