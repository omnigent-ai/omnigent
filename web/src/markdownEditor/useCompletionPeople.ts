// The people an `@` completion offers: the PR's own first, apart from the writer, then whom its
// reviewer picker would list. That list is read only once someone types `@`, and then keeps the
// picker's own lifecycle.

import { useMemo, useState } from "react";

import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import {
  type LeadingPeople,
  mergePeople,
  type Person,
  preparePeople,
  rankPeople,
} from "@/lib/peopleSearch";
import { useReviewerPeopleSource } from "@/shell/ReviewerPickerList";

import { COMPLETION_LIMIT } from "./markdownCompletion";
import type { MarkdownCompletionSource } from "./markdownCompletionContext";

const NO_PEOPLE: readonly Person[] = [];

/** The best people for `query` in `source`'s PR; null `query` while no `@` is being completed. */
export function useCompletionPeople(
  source: MarkdownCompletionSource | null,
  query: string | null,
): { people: readonly Person[]; pending: boolean } {
  // Once asked for, the picker's people stay read for as long as this editor is open.
  const [asked, setAsked] = useState(false);
  if (query !== null && source && !asked) setAsked(true);
  const picker = useReviewerPeopleSource(
    source?.conversationId ?? "",
    source?.prUrl ?? "",
    asked,
    query ?? "",
  );
  const owner = source
    ? (parseCodeHostPullRequestUrl(source.prUrl)?.repository.split("/")[0] ?? null)
    : null;
  const prepared = useMemo(() => {
    // A repository's teams come by slug; a mention names them with their organization.
    const assignable = picker.assignable.flatMap((candidate): Person[] =>
      candidate.kind === "user"
        ? [candidate]
        : owner
          ? [{ ...candidate, login: `${owner}/${candidate.login}` }]
          : [],
    );
    return preparePeople(
      mergePeople({
        reviewer: source?.participants ?? NO_PEOPLE,
        requested: picker.requested,
        assignable,
      }),
    );
  }, [owner, picker.assignable, picker.requested, source?.participants]);
  const viewer = source?.viewer?.toLowerCase() ?? null;
  // The PR's people lead, except the writer, who rarely mentions themselves.
  const leads = useMemo<LeadingPeople>(
    () => (entry) => entry.source === "reviewer" && entry.person.login.toLowerCase() !== viewer,
    [viewer],
  );
  const people = useMemo(
    () =>
      query === null
        ? NO_PEOPLE
        : rankPeople(query, prepared, COMPLETION_LIMIT, { leads }).map((entry) => entry.person),
    [prepared, query, leads],
  );
  return { people, pending: query !== null && picker.pending };
}
