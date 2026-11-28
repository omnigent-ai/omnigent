// Moving between a pull request's sections from ⌘K. Commits has its own commands, which clear
// the open commit and focus the list (see `commitCommands`), so they aren't repeated here.

export type SectionCommandId = "show-overview" | "show-comments" | "show-changes";

/** The section each command shows. */
export type SectionCommandTarget = "summary" | "comments" | "changes";

export interface SectionCommandFacts {
  /** The details are on screen with their sections. */
  available: boolean;
  shown: "summary" | "comments" | "commits" | "changes";
  /** A commit is open in Commits, whose own "Show all changes" leads to Changes. */
  commitOpen: boolean;
}

export interface SectionCommand {
  id: SectionCommandId;
  paletteLabel: string;
  keywords: readonly string[];
  target: SectionCommandTarget;
}

const KEYWORDS = ["pull request", "pr", "section", "tab"];

const COMMANDS: readonly SectionCommand[] = [
  {
    id: "show-overview",
    paletteLabel: "Show the pull request’s overview",
    keywords: [...KEYWORDS, "overview", "summary", "description"],
    target: "summary",
  },
  {
    id: "show-comments",
    paletteLabel: "Show the pull request’s comments",
    keywords: [...KEYWORDS, "comments", "conversation", "discussion"],
    target: "comments",
  },
  {
    id: "show-changes",
    paletteLabel: "Show the pull request’s changes",
    keywords: [...KEYWORDS, "changes", "diff", "files"],
    target: "changes",
  },
];

/** The section commands `facts` allow: every section but the one shown. */
export function sectionCommands(facts: SectionCommandFacts): SectionCommand[] {
  if (!facts.available) return [];
  return COMMANDS.filter((command) => {
    if (command.target === facts.shown) return false;
    // Inside a commit, its "Show all changes" is this same move.
    if (command.target === "changes" && facts.shown === "commits" && facts.commitOpen) return false;
    return true;
  });
}
