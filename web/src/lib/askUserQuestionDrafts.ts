// Unsubmitted AskUserQuestion answers keyed by elicitation id, restored on the
// next mount after the card unmounts (Inbox, another session, a reload). An
// in-memory map mirrored to sessionStorage, like sessionDrafts.

export interface AskUserQuestionDraft {
  currentIndex: number;
  selections: Record<string, string | string[]>;
  customSelected: Record<string, boolean>;
  customInputs: Record<string, string>;
}

interface DraftEntry {
  draft: AskUserQuestionDraft;
  /** Question keys whose typed text must stay in memory only. */
  secretKeys: ReadonlySet<string>;
}

const STORAGE_KEY = "omnigent.askUserQuestionDrafts";
// Answering clears a draft; this bounds the ones whose card never renders again.
const MAX_DRAFTS = 20;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// A parsed key that would pollute the plain-object maps below; treat the whole
// stored draft as corrupt rather than assigning onto the prototype chain.
function isUnsafeKey(key: string): boolean {
  return key === "__proto__" || key === "constructor";
}

function readDraft(value: unknown): AskUserQuestionDraft | undefined {
  if (!isRecord(value)) return undefined;
  const { currentIndex, selections, customSelected, customInputs } = value;
  if (typeof currentIndex !== "number" || !Number.isInteger(currentIndex) || currentIndex < 0) {
    return undefined;
  }
  if (!isRecord(selections) || !isRecord(customSelected) || !isRecord(customInputs)) {
    return undefined;
  }
  const draft: AskUserQuestionDraft = {
    currentIndex,
    selections: {},
    customSelected: {},
    customInputs: {},
  };
  for (const [key, selection] of Object.entries(selections)) {
    if (isUnsafeKey(key)) return undefined;
    if (typeof selection === "string") {
      draft.selections[key] = selection;
    } else if (
      Array.isArray(selection) &&
      selection.every((label): label is string => typeof label === "string")
    ) {
      draft.selections[key] = selection;
    } else {
      return undefined;
    }
  }
  for (const [key, selected] of Object.entries(customSelected)) {
    if (isUnsafeKey(key)) return undefined;
    if (typeof selected !== "boolean") return undefined;
    draft.customSelected[key] = selected;
  }
  for (const [key, text] of Object.entries(customInputs)) {
    if (isUnsafeKey(key)) return undefined;
    if (typeof text !== "string") return undefined;
    draft.customInputs[key] = text;
  }
  return draft;
}

function loadDraftsFromStorage(): Map<string, DraftEntry> {
  if (typeof window === "undefined") return new Map();
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return new Map();
    const entries: unknown = JSON.parse(raw);
    if (!isRecord(entries)) return new Map();
    const drafts = new Map<string, DraftEntry>();
    for (const [id, entry] of Object.entries(entries)) {
      const draft = readDraft(entry);
      if (draft) drafts.set(id, { draft, secretKeys: new Set() });
    }
    return drafts;
  } catch {
    return new Map();
  }
}

function storableDraft({ draft, secretKeys }: DraftEntry): AskUserQuestionDraft {
  if (secretKeys.size === 0) return draft;
  const customInputs: Record<string, string> = {};
  for (const [key, text] of Object.entries(draft.customInputs)) {
    if (!secretKeys.has(key)) customInputs[key] = text;
  }
  return { ...draft, customInputs };
}

function saveDraftsToStorage(): void {
  if (typeof window === "undefined") return;
  try {
    if (drafts.size === 0) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return;
    }
    const entries: Record<string, AskUserQuestionDraft> = Object.create(null);
    for (const [id, entry] of drafts) entries[id] = storableDraft(entry);
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Storage full or unavailable — drafts still work in-memory.
  }
}

const drafts = loadDraftsFromStorage();

/** True when nothing differs from a freshly rendered form. */
function isEmptyDraft(draft: AskUserQuestionDraft): boolean {
  return (
    draft.currentIndex === 0 &&
    Object.values(draft.selections).every((selection) => selection.length === 0) &&
    Object.values(draft.customSelected).every((selected) => !selected) &&
    Object.values(draft.customInputs).every((text) => text === "")
  );
}

export function getAskUserQuestionDraft(elicitationId: string): AskUserQuestionDraft | undefined {
  return drafts.get(elicitationId)?.draft;
}

/** Save a draft; typed text under `secretKeys` never reaches sessionStorage. */
export function setAskUserQuestionDraft(
  elicitationId: string,
  draft: AskUserQuestionDraft,
  secretKeys: Iterable<string> = [],
): void {
  if (isEmptyDraft(draft)) {
    clearAskUserQuestionDraft(elicitationId);
    return;
  }
  // Re-insert so Map order runs from least to most recently updated.
  drafts.delete(elicitationId);
  drafts.set(elicitationId, { draft, secretKeys: new Set(secretKeys) });
  for (const id of drafts.keys()) {
    if (drafts.size <= MAX_DRAFTS) break;
    drafts.delete(id);
  }
  saveDraftsToStorage();
}

export function clearAskUserQuestionDraft(elicitationId: string): void {
  if (!drafts.delete(elicitationId)) return;
  saveDraftsToStorage();
}

/** Clear every draft at once; used to reset module state between tests. */
export function clearAskUserQuestionDrafts(): void {
  drafts.clear();
  inFlightApprovals.clear();
  saveDraftsToStorage();
}

// Elicitation ids whose approval POST has not settled. The optimistic flip to
// "responded" remounts a card as fresh before the server confirms; this lets
// the remount tell an unconfirmed flip from a committed answer.
const inFlightApprovals = new Map<string, number>();

export function markApprovalInFlight(elicitationId: string): void {
  inFlightApprovals.set(elicitationId, (inFlightApprovals.get(elicitationId) ?? 0) + 1);
}

export function clearApprovalInFlight(elicitationId: string): void {
  const count = inFlightApprovals.get(elicitationId);
  if (count === undefined) return;
  if (count > 1) {
    inFlightApprovals.set(elicitationId, count - 1);
  } else {
    inFlightApprovals.delete(elicitationId);
  }
}

export function isApprovalInFlight(elicitationId: string): boolean {
  return inFlightApprovals.has(elicitationId);
}
