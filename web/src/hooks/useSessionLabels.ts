import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { newSessionLabels } from "@/lib/sessionLabels";
import { overlayLabelsIntoCaches } from "@/lib/sessionListCache";
import { updateSession } from "@/lib/sessionsApi";

/**
 * Set or delete a session's user labels via `PATCH /v1/sessions/{id}`. A
 * `null` value deletes that label. The server-confirmed label map is then
 * painted onto every cache that renders the session.
 *
 * @param sessionId - The session to edit, e.g. `"conv_abc123"`.
 */
export function useUpdateSessionLabels(sessionId: string | null | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: Record<string, string | null>) => {
      if (!sessionId) throw new Error("No session selected");
      return updateSession(sessionId, { labels: changes });
    },
    onSuccess: (updated) => overlayLabelsIntoCaches(queryClient, updated.id, updated.labels ?? {}),
  });
}

/**
 * Labels for a session not created yet: the project's defaults plus this
 * session's edits. Removing a default only leaves it off this session.
 *
 * @param projectDefaults - The selected project's default labels, `{}` without one.
 * @param projectName - The selected project; switching it forgets the removed
 *   defaults (they belonged to the old project) but keeps added labels.
 * @param initialEdits - Edits to start from, e.g. restored from a composer
 *   draft: a value sets a label, `null` leaves a project default off.
 */
export function useNewSessionLabels(
  projectDefaults: Record<string, string>,
  projectName: string,
  initialEdits: Record<string, string | null> = {},
) {
  const [edits, setEdits] = useState<Record<string, string | null>>(() => initialEdits);
  const editedProject = useRef(projectName);
  useEffect(() => {
    if (editedProject.current === projectName) return;
    editedProject.current = projectName;
    setEdits((prev) => Object.fromEntries(Object.entries(prev).filter(([, v]) => v !== null)));
  }, [projectName]);
  const resolved = useMemo(
    () => newSessionLabels(projectDefaults, edits),
    [projectDefaults, edits],
  );
  return {
    ...resolved,
    /** The raw edits, for a draft to restore later. */
    edits,
    setLabel: (key: string, value: string) => setEdits((prev) => ({ ...prev, [key]: value })),
    removeLabel: (key: string) =>
      setEdits((prev) => {
        const rest = Object.fromEntries(Object.entries(prev).filter(([k]) => k !== key));
        return key in projectDefaults ? { ...rest, [key]: null } : rest;
      }),
    reset: () => setEdits({}),
  };
}
