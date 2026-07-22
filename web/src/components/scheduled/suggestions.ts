// Static "Suggestions" shown below the scheduled-tasks list.
//
// NOTE: This list is hardcoded PENDING a suggestions backend endpoint. There is
// no /v1/scheduled-tasks/suggestions route today; when one lands, replace this
// module with a hook that fetches it. Selecting a suggestion is wired to
// prefill the manual create dialog (name + prompt); the schedule is left at the
// form default for the user to confirm.

import type { LucideIcon } from "lucide-react";
import { BugIcon, NewspaperIcon } from "lucide-react";

export interface ScheduledTaskSuggestion {
  id: string;
  icon: LucideIcon;
  /** Short chip label (1-2 words) shown on the pill. The fuller name used for
   *  the created task lives in `prefill.name`, not here. */
  title: string;
  description: string;
  /** Prefill applied to the manual create dialog when the suggestion is picked. */
  prefill: { name: string; prompt: string };
}

export const SCHEDULED_TASK_SUGGESTIONS: ScheduledTaskSuggestion[] = [
  // Chips render in array order — "Daily brief" first, then "Issue triage".
  {
    id: "daily-brief",
    icon: NewspaperIcon,
    title: "Daily brief",
    description: "Every morning, summarize overnight activity into a short brief.",
    prefill: {
      name: "Daily morning brief",
      prompt:
        "Put together a short morning brief: summarize new issues, pull requests, and notable activity since yesterday, and call out anything that needs my attention today.",
    },
  },
  {
    id: "morning-triage",
    icon: BugIcon,
    title: "Issue triage",
    description: "Every weekday morning, summarize new issues and flag anything urgent.",
    prefill: {
      name: "Morning issue triage",
      prompt:
        "Review issues opened since yesterday. Summarize the important ones and flag anything that looks urgent or is blocking a release.",
    },
  },
];
