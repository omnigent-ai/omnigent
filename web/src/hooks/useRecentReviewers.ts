// The people this Mac's account worked with recently in a PR's GitHub organization, most
// first, so the reviewer picker can offer them before everyone else. Logins only; names come
// from the picker's other sources. A ranking hint: without it, the picker ranks as before.

import { useMemo } from "react";

import {
  useMacOrganizationList,
  type MacOrganizationListProgress,
} from "@/hooks/useMacOrganizationList";
import type { Person } from "@/lib/peopleSearch";

export const RECENT_REVIEWERS_OPERATION = "recent_reviewers";

/** The shell's answer for one organization. */
export interface RecentReviewersAnswer extends MacOrganizationListProgress {
  object: "list";
  organization: string;
  data: { login: string }[];
  complete: boolean;
  fetched_at: string | null;
  failure: { code: string } | null;
}

const NO_PEOPLE: Person[] = [];

/** The people `prUrl`'s organization's recent list names, while `enabled` and offered. */
export function useRecentReviewers(prUrl: string, enabled: boolean): Person[] {
  const { answer } = useMacOrganizationList<RecentReviewersAnswer>(
    RECENT_REVIEWERS_OPERATION,
    prUrl,
    enabled,
  );
  // One array per answer, so the picker folds its people again only when it changed.
  return useMemo(
    () =>
      answer
        ? answer.data.map(({ login }) => ({ login, name: null, kind: "user" as const }))
        : NO_PEOPLE,
    [answer],
  );
}
