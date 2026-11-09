import type { LedgerEntry, LedgerPage } from "@/lib/sessionLedger";

/** A ledger entry at *position*; a note unless *fields* say otherwise. */
export function ledgerEntry(position: number, fields: Partial<LedgerEntry> = {}): LedgerEntry {
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
export function ledgerPlan(position: number, ...steps: [string, string[]?][]): LedgerEntry {
  return ledgerEntry(position, {
    kind: "plan",
    payload: {
      name: "The plan",
      version: position + 1,
      source: "agent",
      workflow: null,
      steps: steps.map(([id, depends_on = []]) => ({ id, name: id, goal: null, depends_on })),
    },
  });
}

/** One page of a ledger, as `GET …/ledger` returns it. */
export function ledgerPage(data: LedgerEntry[], fields: Partial<LedgerPage> = {}): LedgerPage {
  return {
    object: "list",
    data,
    first_id: data[0]?.id ?? null,
    last_id: data.at(-1)?.id ?? null,
    has_more: false,
    ...fields,
  };
}
