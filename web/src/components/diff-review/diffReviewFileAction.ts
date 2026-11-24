// What a diff review offers for one file: the shape its header menu, tree rows and ⌘K share.

import type { LucideIcon } from "lucide-react";

/**
 * Something the person can do with one file, offered in its header's menu and
 * ⌘K. The "copy-path" action shows as the copy button beside the file's name,
 * and in the menu only while a narrow header hides that button.
 */
export interface DiffReviewFileAction {
  id: string;
  label: string;
  /** ⌘K's name for it, when that differs from the menu's. */
  paletteLabel?: string;
  icon: LucideIcon;
  /** Extra terms ⌘K matches when it offers this action for the review's current file. */
  keywords?: readonly string[];
  /** Why it can't run right now; the row stays listed, greyed out, with this reason. */
  disabledReason?: string;
  /** Where it goes, for an action that opens a page: its menu row is that link. */
  link?: { href: string; follow: (event: MouseEvent) => void };
  run: () => void;
}
