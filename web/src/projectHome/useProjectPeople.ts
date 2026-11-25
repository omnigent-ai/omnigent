import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useViewerId } from "@/hooks/useViewerId";
import { listProjectShares } from "@/lib/projectsApi";
import { personDisplayName, type MentionForm } from "./personaMentions";
import { useProjectIdentities } from "./useProjectIdentities";

/** A project member the viewer can cc, and the `@` form to insert for them. */
export interface ProjectPerson {
  userId: string;
  name: string;
  /** What autocomplete inserts: the display name, or the sign-in name when that is ambiguous. */
  mention: string;
}

/** The owner and shared members, other than the viewer; empty when nothing is shared. */
export function useProjectPeople(projectId: string | null, personaNames: readonly string[]) {
  const viewerId = useViewerId();
  const shares = useQuery({
    // Shares the sharing dialog's cache entry.
    queryKey: ["project-shares", projectId],
    queryFn: () => listProjectShares(projectId as string),
    enabled: projectId !== null,
    staleTime: 60_000,
    retry: false,
  });
  const ids = shares.data
    ? [shares.data.owner_user_id, ...shares.data.data.map((share) => share.user_id)]
    : [];
  const members = [...new Set(ids.filter((id): id is string => !!id))];
  if (members.length < 2) return [];
  const taken = new Set(personaNames.map((name) => name.toLowerCase()));
  // The server matches a name only when no other member or persona shares it.
  const counts = new Map<string, number>();
  const count = (key: string) => counts.set(key, (counts.get(key) ?? 0) + 1);
  for (const id of members) {
    count(personDisplayName(id).toLowerCase());
    const local = localPart(id).toLowerCase();
    if (local !== personDisplayName(id).toLowerCase()) count(local);
  }
  const unique = (key: string) => !taken.has(key) && counts.get(key) === 1;
  return members
    .filter((id) => id !== viewerId)
    .map((userId): ProjectPerson => {
      const name = personDisplayName(userId);
      const local = localPart(userId);
      const mention = unique(name.toLowerCase())
        ? name
        : unique(local.toLowerCase())
          ? local
          : userId;
      return { userId, name, mention };
    });
}

function localPart(userId: string): string {
  return userId.split("@", 1)[0] ?? userId;
}

/**
 * Every `@` form that names one of the project's team members or people, for rendering
 * mentions as pills. Mirrors the server's matching: a person matches their sign-in ID
 * and its local part, and their display and first name only when nobody else shares
 * them; a team member's name always wins.
 */
export function useMentionForms(projectId: string | null): MentionForm[] {
  const identities = useProjectIdentities(projectId);
  const shares = useQuery({
    queryKey: ["project-shares", projectId],
    queryFn: () => listProjectShares(projectId as string),
    enabled: projectId !== null,
    staleTime: 60_000,
    retry: false,
  });
  return useMemo(() => {
    const personas = (identities.data ?? []).map((identity) => identity.name);
    const ids = shares.data
      ? [shares.data.owner_user_id, ...shares.data.data.map((share) => share.user_id)]
      : [];
    return mentionForms(
      personas,
      ids.filter((id): id is string => !!id),
    );
  }, [identities.data, shares.data]);
}

/** See {@link useMentionForms}; exported for tests. */
export function mentionForms(
  personas: readonly string[],
  people: readonly string[],
): MentionForm[] {
  const taken = new Set(personas.map((name) => name.toLowerCase()));
  const forms = new Map<string, string[]>();
  for (const id of new Set(people)) {
    const display = personDisplayName(id);
    forms.set(id, [...new Set([id, localPart(id), display, display.split(" ", 1)[0] ?? display])]);
  }
  const owners = new Map<string, Set<string>>();
  for (const [id, names] of forms)
    for (const name of names) {
      const key = name.toLowerCase();
      owners.set(key, (owners.get(key) ?? new Set()).add(id));
    }
  const result: MentionForm[] = personas.map((name) => ({ form: name, label: name }));
  for (const [id, names] of forms)
    for (const name of names) {
      const key = name.toLowerCase();
      if (taken.has(key) || (owners.get(key)?.size ?? 0) > 1) continue;
      result.push({ form: name, label: personDisplayName(id) });
    }
  return result;
}
