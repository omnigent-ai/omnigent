// A checkbox picker's draft for one PR: checks made while it's open, sent as one change once it
// closes. Checks belong to who made them.

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  EMPTY_PICKER_DRAFT,
  type PickerDraft,
  type PickerDraftChange,
  pickerDraftChange,
  pickerDraftChecked,
  pickerDraftInScope,
  type PickerDraftScope,
  togglePickerDraft,
} from "@/lib/pickerDraft";

export interface PickerDraftState<Target> {
  /** The draft for the PR the picker edits now; empty once its scope moved. */
  draft: PickerDraft<Target>;
  /** What the picker shows checked. */
  checked: ReadonlySet<string>;
  /** What closing will send, or null when nothing would be. */
  change: PickerDraftChange<Target> | null;
  toggle: (target: Target) => void;
}

/**
 * The draft for `prUrl`, whose chosen items are `current` (keyed by `keyOf`). Once the picker
 * is closed and no change is `waiting`, the draft goes to `send` as one change. A change of
 * workspace user drops it, even if it changes back.
 */
export function usePickerDraft<Target>({
  prUrl,
  current,
  open,
  waiting,
  keyOf,
  send,
}: {
  /** The PR the picker edits; null while it can't edit one. */
  prUrl: string | null;
  current: ReadonlySet<string>;
  open: boolean;
  waiting: boolean;
  keyOf: (target: Target) => string;
  send: (change: PickerDraftChange<Target>) => void;
}): PickerDraftState<Target> {
  const viewerId = useSyncExternalStore(subscribeIdentity, getCurrentUserId, () => null);
  const scope = useMemo<PickerDraftScope>(() => ({ owner: viewerId }), [viewerId]);
  const [stored, setStored] = useState<PickerDraft<Target>>(EMPTY_PICKER_DRAFT);
  const inScope = !!stored.prUrl && pickerDraftInScope(stored, scope);
  const draft = inScope ? stored : EMPTY_PICKER_DRAFT;
  // Out of scope once is out for good: switching back doesn't revive the checks.
  useEffect(() => {
    if (!inScope && stored !== EMPTY_PICKER_DRAFT) setStored(EMPTY_PICKER_DRAFT);
  }, [inScope, stored]);
  const checked = useMemo(
    () => (draft.prUrl === prUrl ? pickerDraftChecked(current, draft) : current),
    [current, draft, prUrl],
  );
  const toggle = (target: Target) => {
    if (!prUrl) return;
    setStored((previous) =>
      togglePickerDraft(previous, prUrl, scope, current, keyOf(target), target),
    );
  };
  const sendRef = useRef(send);
  sendRef.current = send;
  // The draft last sent: a render before the reset lands mustn't send it a second time.
  const sentRef = useRef<PickerDraft<Target> | null>(null);
  useEffect(() => {
    if (open || waiting || draft.wanted.size === 0 || sentRef.current === draft) return;
    sentRef.current = draft;
    // Another PR's draft can't be compared with this one's items: it only adds, and adding
    // what's already there changes nothing.
    const change = pickerDraftChange(draft.prUrl === prUrl ? current : new Set<string>(), draft);
    setStored(EMPTY_PICKER_DRAFT);
    if (change) sendRef.current(change);
  }, [current, draft, open, prUrl, waiting]);
  return {
    draft,
    checked,
    change: draft.prUrl === prUrl ? pickerDraftChange(current, draft) : null,
    toggle,
  };
}
