// What one PR's checkbox picker will change once it closes: each item checked or unchecked
// while it was open. Closing sends it all as one change, so checking several never waits on
// GitHub between clicks. The reviewer and label pickers share this.

/** Who a draft's checks were made as: the workspace user. Another scope never sends them. */
export interface PickerDraftScope {
  owner: string | null;
}

export function samePickerDraftScope(left: PickerDraftScope, right: PickerDraftScope): boolean {
  return left.owner === right.owner;
}

/** Each toggled item and whether it should end up chosen, for one PR and scope. */
export interface PickerDraft<Target> {
  prUrl: string | null;
  scope: PickerDraftScope | null;
  wanted: ReadonlyMap<string, { target: Target; chosen: boolean }>;
}

export const EMPTY_PICKER_DRAFT: PickerDraft<never> = {
  prUrl: null,
  scope: null,
  wanted: new Map(),
};

/** The one change a draft sends: what to add and what to remove on its PR. */
export interface PickerDraftChange<Target> {
  prUrl: string;
  add: Target[];
  remove: Target[];
}

/** Whether `draft` still belongs to `scope`, the one its PR is changed as now. */
export function pickerDraftInScope<Target>(
  draft: PickerDraft<Target>,
  scope: PickerDraftScope,
): boolean {
  return !!draft.scope && samePickerDraftScope(draft.scope, scope);
}

/** What the picker shows checked: what's chosen now, as the draft changes it. */
export function pickerDraftChecked<Target>(
  current: ReadonlySet<string>,
  draft: PickerDraft<Target>,
): ReadonlySet<string> {
  if (draft.wanted.size === 0) return current;
  const checked = new Set(current);
  for (const [key, { chosen }] of draft.wanted) {
    if (chosen) checked.add(key);
    else checked.delete(key);
  }
  return checked;
}

/**
 * Flip `target`, keyed `key`, on `prUrl` as `scope`; a flip back to what's chosen now leaves
 * nothing to send. Another PR's or scope's checks are dropped, not carried over.
 */
export function togglePickerDraft<Target>(
  draft: PickerDraft<Target>,
  prUrl: string,
  scope: PickerDraftScope,
  current: ReadonlySet<string>,
  key: string,
  target: Target,
): PickerDraft<Target> {
  const kept = draft.prUrl === prUrl && pickerDraftInScope(draft, scope);
  const wanted = new Map(kept ? draft.wanted : []);
  const checked = wanted.get(key)?.chosen ?? current.has(key);
  if (!checked === current.has(key)) wanted.delete(key);
  else wanted.set(key, { target, chosen: !checked });
  return { prUrl, scope, wanted };
}

/** The one change that makes `current` what the draft wants, or null when it already is. */
export function pickerDraftChange<Target>(
  current: ReadonlySet<string>,
  draft: PickerDraft<Target>,
): PickerDraftChange<Target> | null {
  if (!draft.prUrl) return null;
  const add: Target[] = [];
  const remove: Target[] = [];
  for (const [key, { target, chosen }] of draft.wanted) {
    if (chosen && !current.has(key)) add.push(target);
    if (!chosen && current.has(key)) remove.push(target);
  }
  return add.length > 0 || remove.length > 0 ? { prUrl: draft.prUrl, add, remove } : null;
}

/** How a picker's footer words its draft: "1 to request, 1 to withdraw · sent when you close". */
export interface PickerDraftWords {
  add: string;
  remove: string;
  sends: string;
}

/** What closing the picker will send, as its footer says it; null when nothing would be. */
export function pickerDraftSummary(
  change: Pick<PickerDraftChange<unknown>, "add" | "remove"> | null,
  words: PickerDraftWords,
): string | null {
  if (!change) return null;
  const parts = [
    change.add.length > 0 && `${change.add.length} ${words.add}`,
    change.remove.length > 0 && `${change.remove.length} ${words.remove}`,
  ].filter(Boolean);
  return parts.length > 0 ? `${parts.join(", ")} · ${words.sends}` : null;
}

/** Everything the draft checks, so an item no list names stays shown while it's checked. */
export function pickerDraftTargets<Target>(draft: PickerDraft<Target>): Target[] {
  return [...draft.wanted.values()].flatMap(({ target, chosen }) => (chosen ? [target] : []));
}
