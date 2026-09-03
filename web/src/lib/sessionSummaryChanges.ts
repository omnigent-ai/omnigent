let revision = 0;
const listeners = new Set<() => void>();

export function getSessionSummaryRevision(): number {
  return revision;
}

export function subscribeSessionSummaryChanges(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function notifySessionSummariesMayHaveChanged(): void {
  revision += 1;
  for (const listener of listeners) listener();
}
