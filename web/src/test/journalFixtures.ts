import type { JournalEntry, JournalPage } from "@/lib/sessionJournal";

/** A journal entry at *position*; a note unless *fields* say otherwise. */
export function journalEntry(position: number, fields: Partial<JournalEntry> = {}): JournalEntry {
  return {
    id: `e${position}`,
    position,
    created_at: 1_762_534_860 + position * 60,
    scheduled_task_run_id: null,
    step: null,
    author: { type: "agent" },
    kind: "note",
    payload: { body: `note ${position}` },
    ...fields,
  };
}

/** A plan entry named "The plan", whose steps are `[id, dependencies]` pairs. */
export function journalPlan(position: number, ...steps: [string, string[]?][]): JournalEntry {
  return journalEntry(position, {
    kind: "plan",
    payload: {
      name: "The plan",
      version: position + 1,
      source: "agent",
      steps: steps.map(([id, depends_on = []]) => ({ id, name: id, goal: null, depends_on })),
    },
  });
}

/** One page of a journal, as `GET …/journal` returns it. */
export function journalPage(data: JournalEntry[], fields: Partial<JournalPage> = {}): JournalPage {
  return {
    object: "list",
    data,
    first_id: data[0]?.id ?? null,
    last_id: data.at(-1)?.id ?? null,
    has_more: false,
    ...fields,
  };
}
