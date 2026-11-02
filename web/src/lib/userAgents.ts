/** One current agent per exact name, including responses from older servers. */
export function currentUserAgents<
  T extends { id: string; name: string; created_at?: number | null },
>(agents: readonly T[]): T[] {
  const byName = new Map<string, T>();
  for (const agent of agents) {
    const existing = byName.get(agent.name);
    const created = agent.created_at ?? 0;
    const previous = existing?.created_at ?? 0;
    if (!existing || created > previous || (created === previous && agent.id > existing.id)) {
      byName.set(agent.name, agent);
    }
  }
  return [...byName.values()];
}
