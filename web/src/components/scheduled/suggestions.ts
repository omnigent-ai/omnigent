// Static "Suggestions" shown below the scheduled-tasks list.
//
// Selecting a suggestion prefills the manual create dialog. The schedule is
// left at the form default for the user to confirm.

import type { LucideIcon } from "lucide-react";
import { CalendarClockIcon, GitPullRequestIcon, NewspaperIcon } from "lucide-react";

export interface ScheduledTaskSuggestion {
  id: string;
  icon: LucideIcon;
  iconClassName: string;
  /** Short chip label (1-2 words) shown on the pill. The fuller name used for
   *  the created task lives in `prefill.name`, not here. */
  title: string;
  /** Prefill applied to the manual create dialog when the suggestion is picked. */
  prefill: { name: string; prompt: string };
}

export const SCHEDULED_TASK_SUGGESTIONS: ScheduledTaskSuggestion[] = [
  {
    id: "follow-up-monitor",
    icon: CalendarClockIcon,
    iconClassName: "text-blue-600 dark:text-blue-400",
    title: "Follow-up monitor",
    prefill: {
      name: "Follow-up monitor",
      prompt:
        "Review recent email and calendar activity every weekday morning. Summarize anything that needs my attention and call out follow-ups I should handle today.",
    },
  },
  {
    id: "pr-sweep",
    icon: GitPullRequestIcon,
    iconClassName: "text-emerald-600 dark:text-emerald-500",
    title: "PR sweep",
    prefill: {
      name: "PR sweep",
      prompt:
        "List open pull requests waiting on review. Highlight stale PRs that need a nudge and summarize the next action for each one.",
    },
  },
  {
    id: "news-digest",
    icon: NewspaperIcon,
    iconClassName: "text-amber-600 dark:text-amber-500",
    title: "News digest",
    prefill: {
      name: "News digest",
      prompt:
        "Summarize notable news from the last day. Keep it concise, group related items, and flag anything worth reading more closely.",
    },
  },
];
