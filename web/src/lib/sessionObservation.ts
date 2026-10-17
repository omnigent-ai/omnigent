/** Durable ordering for lifecycle effects across runner and response changes. */
export interface SessionObservation {
  runner_id: string | null;
  binding_epoch: number;
  revision: number;
  observation_id: string | null;
  status: string | null;
  response_id: string | null;
  error_key: string | null;
}

export function parseSessionObservation(raw: unknown): SessionObservation | undefined {
  if (raw === null || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  if (
    (value.runner_id != null && typeof value.runner_id !== "string") ||
    !Number.isSafeInteger(value.binding_epoch) ||
    (value.binding_epoch as number) < 0 ||
    !Number.isSafeInteger(value.revision) ||
    (value.revision as number) < 0
  ) {
    return undefined;
  }
  for (const key of ["observation_id", "status", "response_id", "error_key"]) {
    if (value[key] != null && typeof value[key] !== "string") return undefined;
  }
  const optionalText = (key: string): string | null =>
    typeof value[key] === "string" ? value[key] : null;
  return {
    runner_id: (value.runner_id as string | null | undefined) ?? null,
    binding_epoch: value.binding_epoch as number,
    revision: value.revision as number,
    observation_id: optionalText("observation_id"),
    status: optionalText("status"),
    response_id: optionalText("response_id"),
    error_key: optionalText("error_key"),
  };
}

export function isVersionedSessionObservation(
  observation: SessionObservation | null | undefined,
): observation is SessionObservation {
  return (
    observation != null &&
    (observation.binding_epoch > 0 ||
      observation.revision > 0 ||
      observation.observation_id != null)
  );
}

/** Mirrors entities.session_observation.accept_session_projection. */
export function acceptSessionProjection(
  incoming: SessionObservation | null | undefined,
  incomingEpisodeId: string | null | undefined,
  previous: SessionObservation | null | undefined,
  previousEpisodeId: string | null | undefined,
): boolean {
  if (!isVersionedSessionObservation(previous)) return true;
  if (!isVersionedSessionObservation(incoming)) return false;
  if (incoming.binding_epoch !== previous.binding_epoch) {
    return incoming.binding_epoch > previous.binding_epoch;
  }
  if (incoming.revision !== previous.revision) return incoming.revision > previous.revision;
  const sameObservation =
    (incoming.runner_id ?? null) === (previous.runner_id ?? null) &&
    (incoming.observation_id ?? null) === (previous.observation_id ?? null) &&
    (incoming.status ?? null) === (previous.status ?? null) &&
    (incoming.response_id ?? null) === (previous.response_id ?? null) &&
    (incoming.error_key ?? null) === (previous.error_key ?? null);
  if (!sameObservation) return false;
  const incomingOwned = incomingEpisodeId != null;
  const previousOwned = previousEpisodeId != null;
  if (incomingOwned !== previousOwned) return incomingOwned;
  return (incomingEpisodeId ?? null) === (previousEpisodeId ?? null);
}

const SESSION_ERROR_LABEL_KEYS = new Set(
  ["code", "message", "agent_name", "title", "cause", "remediation", "item_id", "runner_id"].map(
    (name) => `omnigent.last_task_error_${name}`,
  ),
);

/** Keep owned error labels while still accepting independent user metadata. */
export function preserveSessionErrorLabels(
  incoming: Record<string, string>,
  previous: Record<string, string> | undefined,
): Record<string, string> {
  return {
    ...Object.fromEntries(
      Object.entries(incoming).filter(([key]) => !SESSION_ERROR_LABEL_KEYS.has(key)),
    ),
    ...Object.fromEntries(
      Object.entries(previous ?? {}).filter(([key]) => SESSION_ERROR_LABEL_KEYS.has(key)),
    ),
  };
}
