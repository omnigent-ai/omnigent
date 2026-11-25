// A changed file as a ⌘K command: open it in a tab of its own, behind this one, as
// ⌘-click on a file row does. Every review surface takes its label from here.

import { AVAILABLE, type CommandAvailability } from "@/store/paletteCommands";

export type ChangedFileCommandId = "open-file-in-new-tab";

export interface ChangedFileCommandFacts {
  /** The changed file on screen, if any. */
  shownPath: string | null;
  /** Files can open in a tab of their own, as in the tabs sidebar. */
  newTab: boolean;
  /** Words naming the surface, such as "pull request", that ⌘K also matches. */
  sourceKeywords?: readonly string[];
}

export interface ChangedFileCommand {
  id: ChangedFileCommandId;
  paletteLabel: string;
  keywords: readonly string[];
  availability: CommandAvailability;
  /** Named after the file, so sessions matching its name stay above it in ⌘K. */
  leadsSources: false;
  path: string;
}

/** The commands `facts` allows, in the order ⌘K lists them. */
export function changedFileCommands(facts: ChangedFileCommandFacts): ChangedFileCommand[] {
  if (!facts.newTab || facts.shownPath === null) return [];
  return [
    {
      id: "open-file-in-new-tab",
      paletteLabel: `Open ${facts.shownPath} in a new tab`,
      keywords: [
        "file",
        "changes",
        "diff",
        ...(facts.sourceKeywords ?? []),
        "tab",
        "new",
        "background",
      ],
      availability: AVAILABLE,
      leadsSources: false,
      path: facts.shownPath,
    },
  ];
}
