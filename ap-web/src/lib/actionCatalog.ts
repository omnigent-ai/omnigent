/**
 * Action catalog — the groups of ready-made steps the builder's "+" menu offers
 * alongside the generic node types.
 *
 * Groups are now sourced from two real, persisted places (no more hardcoded
 * mock):
 *
 *  - **Entities** (`entityStore`) — saved `{id, title, instruction}` building
 *    blocks, e.g. the Jira actions. Grouped under "Entities".
 *  - **Jobs** (`jobsStore`) — every existing job, usable as a step in another
 *    flow (its instruction is the job's narrative). Grouped under "Jobs". The
 *    current job is excluded to avoid wiring a flow into itself.
 *
 * Each pickable item is an {@link ActionDef} carrying the `instruction` text
 * that gets folded into the flow when used; a step records the action's id +
 * group, and the builder uses the instruction as the step label/source.
 */

import { useMemo } from "react";
import { useEntities } from "@/lib/entityStore";
import { useEntityGroups } from "@/lib/entityGroupStore";
import { useJobs } from "@/lib/jobsStore";
import { treeToGraph } from "@/lib/flowTree";
import { generateFlowText } from "@/lib/flowToText";

/** How a group renders its icon: a bundled component (built-ins) or an image URL. */
export type ActionGroupIcon =
  | { kind: "component"; key: string }
  | { kind: "url"; url: string };

export interface ActionDef {
  /** Stable identifier, e.g. an entity id or "job:<jobId>". */
  id: string;
  /** Display label shown in the step box. */
  label: string;
  /** Short description (tooltip). */
  description?: string;
  /** Instruction text folded into the flow narrative when used. */
  instruction: string;
}

export interface ActionGroup {
  /** Stable group id, e.g. an entity-group id, "entities", or "jobs". */
  id: string;
  /** Display name. */
  name: string;
  /** Group icon, resolved by the picker; absent for the fallback/Jobs groups. */
  icon?: ActionGroupIcon;
  actions: ActionDef[];
}

/**
 * Reactive catalog: saved entities + existing jobs, as pickable groups.
 *
 * @param excludeJobId - a job id to omit from the Jobs group (the job being
 *   edited, so it can't be wired into itself).
 */
export function useActionCatalog(excludeJobId?: string): {
  groups: ActionGroup[];
  loading: boolean;
} {
  const entities = useEntities();
  const entityGroups = useEntityGroups();
  const jobs = useJobs();

  const groups = useMemo<ActionGroup[]>(() => {
    const out: ActionGroup[] = [];

    // Bucket entities by their group id (null = ungrouped).
    const byGroup = new Map<string, typeof entities>();
    const ungrouped: typeof entities = [];
    for (const e of entities) {
      if (e.groupId) {
        const list = byGroup.get(e.groupId) ?? [];
        list.push(e);
        byGroup.set(e.groupId, list);
      } else {
        ungrouped.push(e);
      }
    }

    const toAction = (e: (typeof entities)[number]) => ({
      id: e.id,
      label: e.title,
      description: e.instruction,
      instruction: e.instruction,
    });

    // One ActionGroup per entity group (built-ins first, per the store's sort),
    // each carrying its icon. Empty groups are omitted from the picker.
    for (const g of entityGroups) {
      const members = byGroup.get(g.id) ?? [];
      if (!members.length) continue;
      const icon: ActionGroupIcon | undefined = g.iconKey
        ? { kind: "component", key: g.iconKey }
        : g.iconUrl
          ? { kind: "url", url: g.iconUrl }
          : undefined;
      out.push({ id: g.id, name: g.name, icon, actions: members.map(toAction) });
    }

    // Entities not assigned to any group fall back to a plain "Entities" group.
    if (ungrouped.length) {
      out.push({ id: "entities", name: "Entities", actions: ungrouped.map(toAction) });
    }

    const jobActions = jobs
      .filter((j) => j.id !== excludeJobId)
      .map((j) => {
        // A job's instruction is the narrative rendered from its flow.
        const instruction =
          generateFlowText(treeToGraph(j.tree)).narrative || `Run the “${j.name}” flow.`;
        return {
          id: `job:${j.id}`,
          label: j.name,
          description: "Existing job, wired in as a step.",
          instruction,
        };
      });
    if (jobActions.length) {
      out.push({ id: "jobs", name: "Jobs", actions: jobActions });
    }

    return out;
  }, [entities, entityGroups, jobs, excludeJobId]);

  // Sourced from local stores/hooks — always ready, no async load.
  return { groups, loading: false };
}
