import { type QueryClient, skipToken, useQuery } from "@tanstack/react-query";
import type { Conversation } from "@/hooks/useConversations";
import type { Session } from "./types";
import { CLIENT_CREATE_TOKEN_LABEL, TEMP_CONV_ID_PREFIX } from "./tempConversationId";
import { PINNED_LABEL_KEY, PROJECT_LABEL_KEY } from "./sessionListCache";

const EMPTY_DRAFTS: readonly NestedSessionDraft[] = [];
const DRAFTS_KEY = ["nested-session-drafts"] as const;
const sidebarKeys = new Map<string, string>();

export interface NestedSessionDraft {
  id: string;
  key: string;
  source: Conversation;
  creating: boolean;
  created: boolean;
  createdAt: number;
}

export function useNestedSessionDrafts(): readonly NestedSessionDraft[] {
  return (
    useQuery<readonly NestedSessionDraft[]>({
      queryKey: DRAFTS_KEY,
      initialData: EMPTY_DRAFTS,
      queryFn: skipToken,
      enabled: false,
      gcTime: Infinity,
    }).data ?? EMPTY_DRAFTS
  );
}

export function addNestedSessionDraft(client: QueryClient, id: string, source: Session): void {
  client.setQueryData<readonly NestedSessionDraft[]>(DRAFTS_KEY, (drafts = []) => [
    ...drafts.filter((draft) => draft.key !== id),
    {
      id,
      key: id,
      source: {
        id: source.id,
        object: "conversation",
        title: source.title,
        created_at: source.createdAt,
        updated_at: source.createdAt,
        labels: source.labels ?? {},
        permission_level: source.permissionLevel,
        project_id: source.projectId,
        family: source.family ?? {
          id: source.id,
          representative_id: source.id,
          pinned: source.labels?.[PINNED_LABEL_KEY] !== undefined,
          pinned_at: source.labels?.[PINNED_LABEL_KEY],
        },
      },
      creating: false,
      created: false,
      createdAt: Date.now() / 1000,
    },
  ]);
}

export function updateNestedSessionDraft(client: QueryClient, key: string, realId?: string): void {
  if (realId) sidebarKeys.set(realId, key);
  client.setQueryData<readonly NestedSessionDraft[]>(DRAFTS_KEY, (drafts = []) =>
    drafts.map((draft) =>
      draft.key === key
        ? { ...draft, id: realId ?? draft.id, creating: true, created: realId !== undefined }
        : draft,
    ),
  );
}

export function removeNestedSessionDraft(client: QueryClient, key: string): void {
  client.setQueryData<readonly NestedSessionDraft[]>(DRAFTS_KEY, (drafts = []) =>
    drafts.filter((draft) => draft.key !== key),
  );
}

export function nestedSessionSidebarKey(id: string): string {
  return sidebarKeys.get(id) ?? id;
}

/** Overlay local drafts without persisting synthetic family membership. */
export function withNestedSessionDrafts(
  rows: readonly Conversation[],
  drafts: readonly NestedSessionDraft[],
  unavailableIds: ReadonlySet<string> = new Set(),
): Conversation[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  for (const draft of drafts) {
    if (unavailableIds.has(draft.id)) continue;
    const source = byId.get(draft.source.id) ?? draft.source;
    const family = source.family ?? {
      id: source.id,
      representative_id: source.id,
      pinned: source.labels?.[PINNED_LABEL_KEY] !== undefined,
      pinned_at: source.labels?.[PINNED_LABEL_KEY],
    };
    if (!unavailableIds.has(source.id)) byId.set(source.id, { ...source, family });
    else if (!draft.created) continue;
    if (draft.id !== draft.key) byId.delete(draft.key);
    // Keep one row if the push arrives before the create response.
    if (!draft.created) {
      const token = draft.key.slice(TEMP_CONV_ID_PREFIX.length);
      for (const row of rows) {
        if (row.id !== draft.id && row.labels?.[CLIENT_CREATE_TOKEN_LABEL] === token)
          byId.delete(row.id);
      }
    }
    const existing = byId.get(draft.id);
    byId.set(draft.id, {
      ...existing,
      id: draft.id,
      object: "conversation",
      title: existing?.title ?? (draft.created ? null : "New nested agent"),
      created_at: existing?.created_at ?? draft.createdAt,
      // Opening the composer must not move the family above unrelated sessions.
      updated_at: existing?.updated_at ?? source.updated_at,
      labels: {
        ...(!existing?.family && source.labels[PROJECT_LABEL_KEY]
          ? { [PROJECT_LABEL_KEY]: source.labels[PROJECT_LABEL_KEY] }
          : {}),
        ...existing?.labels,
      },
      permission_level: existing ? existing.permission_level : source.permission_level,
      project_id: existing?.family ? existing.project_id : source.project_id,
      family: existing?.family ?? family,
      provisional: !draft.created,
      nested_draft_source_id: draft.creating ? undefined : source.id,
    });
  }
  return [...byId.values()];
}
