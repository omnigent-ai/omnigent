export type ConversationScrollMode =
  "following-bottom" | "restore-pending" | "restoring" | "user-controlled";

interface ConversationScrollState {
  mode: ConversationScrollMode;
  generation: number;
}

const states = new WeakMap<HTMLElement, ConversationScrollState>();

function stateFor(element: HTMLElement): ConversationScrollState {
  let state = states.get(element);
  if (!state) {
    state = { mode: "following-bottom", generation: 0 };
    states.set(element, state);
  }
  return state;
}

export function conversationScrollMode(element: HTMLElement): ConversationScrollMode {
  return stateFor(element).mode;
}

export function beginConversationScrollRestore(element: HTMLElement): number {
  const state = stateFor(element);
  state.generation += 1;
  state.mode = "restore-pending";
  return state.generation;
}

export function markConversationScrollRestoring(element: HTMLElement, generation: number): boolean {
  const state = stateFor(element);
  if (state.generation !== generation || state.mode === "user-controlled") return false;
  state.mode = "restoring";
  return true;
}

export function isCurrentConversationScrollRestore(
  element: HTMLElement,
  generation: number,
): boolean {
  const state = stateFor(element);
  return (
    state.generation === generation &&
    (state.mode === "restore-pending" || state.mode === "restoring")
  );
}

export function takeConversationScrollControl(element: HTMLElement): void {
  const state = stateFor(element);
  state.generation += 1;
  state.mode = "user-controlled";
}

export function followConversationBottom(element: HTMLElement): void {
  const state = stateFor(element);
  state.generation += 1;
  state.mode = "following-bottom";
}

export function isConversationFollowingBottom(element: HTMLElement): boolean {
  return stateFor(element).mode === "following-bottom";
}
