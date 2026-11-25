// Everyone in a PR's GitHub organization, from the list this Mac keeps for its own account.
// The shell answers from memory and fills or refreshes the list in the background, so this
// only re-reads the local answer: often while it fills, rarely once it's ready.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef } from "react";

import { useMacOrganizationList } from "@/hooks/useMacOrganizationList";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import { orgMembersRetryFailure, type OrgMembersStatus, type Person } from "@/lib/peopleSearch";

export const ORG_MEMBERS_OPERATION = "organization_members";

/** The shell's answer for one organization. */
export interface OrgMembersAnswer {
  object: "list";
  organization: string;
  data: { login: string; name: string | null }[];
  state: OrgMembersStatus["state"];
  complete: boolean;
  truncated: boolean;
  fetched_at: string | null;
  next_attempt_at: string | null;
  failure: { code: string } | null;
}

export interface OrgMembers {
  /** Everyone listed so far; empty when this Mac keeps no list for the PR. */
  members: Person[];
  /** What the list's footer should say, or null when there's no list here. */
  status: OrgMembersStatus | null;
  /** Ask the shell to read the list again now, after the user fixed what failed; never throws. */
  retry: () => void;
  /** A retry is on its way. */
  retrying: boolean;
  /** Why the last retry couldn't reach the shell, in words for the picker, or null. */
  retryFailure: string | null;
}

/** The list a retry reads again and where its answer goes. */
interface RetryTarget {
  scope: string;
  queryKey: readonly unknown[];
  github: CodeHostPullRequest;
  organization: string;
}

const NO_MEMBERS: OrgMembers = {
  members: [],
  status: null,
  retry: () => {},
  retrying: false,
  retryFailure: null,
};

/**
 * The people in `prUrl`'s organization, while `enabled`. Only where this Mac reads the
 * PR's GitHub host and its shell keeps the list; elsewhere, nothing, and pickers work
 * from the repository's candidates and typed logins as before.
 */
export function useOrgMembers(prUrl: string, enabled: boolean): OrgMembers {
  const organizationList = useMacOrganizationList<OrgMembersAnswer>(
    ORG_MEMBERS_OPERATION,
    prUrl,
    enabled,
  );
  const { answer, login, query, queryKey } = organizationList;
  const queryClient = useQueryClient();
  // A mutation, so a refused retry lands in its error state rather than escaping as a rejection.
  // Each retry names the list it was for, so its outcome never shows on another one.
  const retryRead = useMutation({
    mutationFn: async (list: RetryTarget) => ({
      list,
      result: await readOnThisMac<OrgMembersAnswer>(list.github, ORG_MEMBERS_OPERATION, {
        organization: list.organization,
        retry: true,
      }),
    }),
    onSuccess: ({ list, result }) => queryClient.setQueryData(list.queryKey, result),
  });
  const scope = JSON.stringify(queryKey);
  const retryTarget: RetryTarget | null = organizationList.target
    ? { scope, queryKey, ...organizationList.target }
    : null;
  const current = useRef(retryTarget);
  current.current = retryTarget;
  const { mutate } = retryRead;
  const retry = useCallback(() => {
    if (current.current) mutate(current.current);
  }, [mutate]);
  const retrying = retryRead.isPending && retryRead.variables?.scope === scope;
  // A failed retry speaks only for its own list, and only until a newer answer arrives.
  const retryFailure =
    retryRead.isError &&
    retryRead.variables?.scope === scope &&
    query.dataUpdatedAt <= retryRead.submittedAt
      ? orgMembersRetryFailure(retryRead.error)
      : null;
  // One array per answer, so pickers fold the list again only when it changed.
  return useMemo(() => {
    if (!answer) return NO_MEMBERS;
    return {
      members: answer.data.map(({ login: member, name }) => ({
        login: member,
        name,
        kind: "user" as const,
      })),
      status: {
        organization: answer.organization,
        state: answer.state,
        complete: answer.complete,
        truncated: answer.truncated,
        failure: answer.failure?.code ?? null,
        login,
      },
      retry,
      retrying,
      retryFailure,
    };
  }, [answer, login, retry, retrying, retryFailure]);
}
