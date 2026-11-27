import type {
  PathStoreAddEvent,
  PathStoreBatchEvent,
  PathStoreCollapseEvent,
  PathStoreEvent,
  PathStoreEventForType,
  PathStoreEventType,
  PathStoreExpandEvent,
  PathStoreSemanticEvent,
} from "./public-types";
import { createTransactionFrame } from "./state";
import type { PathStoreState, TransactionFrame } from "./state";

interface EventInvalidationArgs {
  affectedAncestorIds?: readonly number[];
  affectedNodeIds?: readonly number[];
  projectionChanged: boolean;
}

export function subscribe<TType extends PathStoreEventType | "*">(
  state: PathStoreState,
  type: TType,
  handler: (event: PathStoreEventForType<TType>) => void,
): () => void {
  const rawHandler = handler as (event: PathStoreEvent) => void;
  const existingListeners = state.listeners.get(type);
  if (existingListeners != null) {
    existingListeners.add(rawHandler);
  } else {
    state.listeners.set(type, new Set([rawHandler]));
  }

  return () => {
    const listeners = state.listeners.get(type);
    if (listeners == null) {
      return;
    }

    listeners.delete(rawHandler);
    if (listeners.size === 0) {
      state.listeners.delete(type);
    }
  };
}

export function createAddEvent(args: EventInvalidationArgs & { path: string }): PathStoreAddEvent {
  return {
    affectedAncestorIds: args.affectedAncestorIds ?? [],
    affectedNodeIds: args.affectedNodeIds ?? [],
    canonicalChanged: true,
    operation: "add",
    path: args.path,
    projectionChanged: args.projectionChanged,
    visibleCountDelta: null,
  };
}

export function createExpandEvent(
  args: EventInvalidationArgs & { path: string },
): PathStoreExpandEvent {
  return {
    affectedAncestorIds: args.affectedAncestorIds ?? [],
    affectedNodeIds: args.affectedNodeIds ?? [],
    canonicalChanged: false,
    operation: "expand",
    path: args.path,
    projectionChanged: true,
    visibleCountDelta: null,
  };
}

export function createCollapseEvent(
  args: EventInvalidationArgs & { path: string },
): PathStoreCollapseEvent {
  return {
    affectedAncestorIds: args.affectedAncestorIds ?? [],
    affectedNodeIds: args.affectedNodeIds ?? [],
    canonicalChanged: false,
    operation: "collapse",
    path: args.path,
    projectionChanged: true,
    visibleCountDelta: null,
  };
}

export function finalizeEvent(
  state: PathStoreState,
  previousVisibleCount: number,
  event: PathStoreSemanticEvent,
): PathStoreSemanticEvent {
  return {
    ...event,
    visibleCountDelta: getCurrentVisibleCount(state) - previousVisibleCount,
  };
}

export function batchEvents(state: PathStoreState, run: () => void): void {
  const previousVisibleCount = getCurrentVisibleCount(state);
  const frame = createTransactionFrame();
  state.transactionStack.push(frame);

  try {
    run();
  } catch (error) {
    finishTransaction(state, frame, false);
    throw error;
  }

  finishTransaction(state, frame, true, getCurrentVisibleCount(state) - previousVisibleCount);
}

export function recordEvent(state: PathStoreState, event: PathStoreSemanticEvent): void {
  recordEventNow(state, event);
}

function recordEventNow(state: PathStoreState, event: PathStoreSemanticEvent): void {
  const currentFrame = state.transactionStack[state.transactionStack.length - 1] ?? null;
  if (currentFrame == null) {
    emitEvent(state, event);
    return;
  }

  currentFrame.events.push(event);
  mergeEventMetadataIntoFrame(currentFrame, event);
}

function finishTransaction(
  state: PathStoreState,
  frame: TransactionFrame,
  emit: boolean,
  visibleCountDelta: number | null = null,
): void {
  const poppedFrame = state.transactionStack.pop();
  if (poppedFrame !== frame) {
    throw new Error("Transaction stack underflow");
  }

  if (!emit) {
    return;
  }

  const parentFrame = state.transactionStack[state.transactionStack.length - 1] ?? null;
  if (parentFrame != null) {
    mergeBatchFrameIntoParent(parentFrame, frame);
    return;
  }

  const batchEvent = createBatchEvent(frame, visibleCountDelta);

  emitEvent(state, batchEvent);
}

function createBatchEvent(
  frame: TransactionFrame,
  visibleCountDelta: number | null,
): PathStoreBatchEvent {
  return {
    affectedAncestorIds: [...frame.affectedAncestorIds],
    affectedNodeIds: [...frame.affectedNodeIds],
    canonicalChanged: frame.events.some((event) => event.canonicalChanged),
    events: [...frame.events],
    operation: "batch",
    projectionChanged: frame.events.some((event) => event.projectionChanged),
    visibleCountDelta,
  };
}

function mergeFrameMetadata(target: TransactionFrame, source: TransactionFrame): void {
  for (const nodeId of source.affectedAncestorIds) {
    target.affectedAncestorIds.add(nodeId);
  }

  for (const nodeId of source.affectedNodeIds) {
    target.affectedNodeIds.add(nodeId);
  }
}

function mergeBatchFrameIntoParent(parentFrame: TransactionFrame, frame: TransactionFrame): void {
  for (const event of frame.events) {
    parentFrame.events.push(event);
  }
  mergeFrameMetadata(parentFrame, frame);
}

function mergeEventMetadataIntoFrame(frame: TransactionFrame, event: PathStoreSemanticEvent): void {
  for (const nodeId of event.affectedNodeIds) {
    frame.affectedNodeIds.add(nodeId);
  }

  for (const nodeId of event.affectedAncestorIds) {
    frame.affectedAncestorIds.add(nodeId);
  }
}

function emitEvent(state: PathStoreState, event: PathStoreEvent): void {
  emitEventNow(state, event);
}

function emitEventNow(state: PathStoreState, event: PathStoreEvent): void {
  const specificListeners = state.listeners.get(event.operation);
  specificListeners?.forEach((handler) => handler(event));
  const wildcardListeners = state.listeners.get("*");
  wildcardListeners?.forEach((handler) => handler(event));
}

function getCurrentVisibleCount(state: PathStoreState): number {
  return state.snapshot.nodes[state.snapshot.rootId]?.visibleSubtreeCount ?? 0;
}
