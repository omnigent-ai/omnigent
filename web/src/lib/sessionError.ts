import type { AnyBlock, BlockContext } from "./blocks";

export type LatestSessionError = "error" | "disconnected" | "recovered_disconnect";

export interface LatestActivityErrorWindow {
  state: LatestSessionError | null | undefined;
  boundaryResolved: boolean;
}

function sameCausalBoundary(first: BlockContext, next: BlockContext): boolean {
  return (
    first.responseId === next.responseId && first.turn === next.turn && first.agent === next.agent
  );
}

/** Classify a bounded window and report whether older history can change it. */
export function latestActivityErrorWindow(
  blocks: readonly AnyBlock[],
  hostOnline?: boolean | null,
): LatestActivityErrorWindow {
  let boundary: BlockContext | null = null;
  let failedLifecycle = false;
  let disconnect: LatestSessionError | null = null;
  const resolvedFailure = () => disconnect ?? (failedLifecycle ? "error" : null);

  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    switch (block.type) {
      case "error": {
        if (boundary && !sameCausalBoundary(boundary, block.ctx)) {
          return { state: resolvedFailure(), boundaryResolved: true };
        }
        boundary ??= block.ctx;
        if (block.level === "info") {
          return { state: resolvedFailure(), boundaryResolved: true };
        }
        if (block.code === "runner_disconnected") {
          disconnect = hostOnline === true ? "recovered_disconnect" : "disconnected";
          break;
        }
        return { state: "error", boundaryResolved: true };
      }
      case "text_done":
        if (boundary && !sameCausalBoundary(boundary, block.ctx)) {
          return { state: resolvedFailure(), boundaryResolved: true };
        }
        // Claude's native transcript can persist an API rejection as ordinary
        // assistant text, without an error item or a failed session status.
        return {
          state: /^API Error:\s*\S/.test(block.fullText.trimStart()) ? "error" : resolvedFailure(),
          boundaryResolved: true,
        };
      case "response_end":
        if (boundary && !sameCausalBoundary(boundary, block.ctx)) {
          return { state: resolvedFailure(), boundaryResolved: true };
        }
        boundary ??= block.ctx;
        if (block.status === "failed") failedLifecycle = true;
        break;
      case "response_start":
      case "user_message":
      case "text_chunk":
      case "tool_group":
      case "tool_result":
      case "native_tool":
      case "reasoning_start":
      case "reasoning_chunk":
      case "reasoning_block":
      case "slash_command":
      case "terminal_command":
      case "file":
      case "policy_denied":
      case "routing_decision":
      case "elicitation":
        return { state: resolvedFailure(), boundaryResolved: true };
      case "retry":
      case "compaction_loading":
      case "compaction":
        break;
      default: {
        const exhaustive: never = block;
        return exhaustive;
      }
    }
  }
  return {
    state: boundary ? resolvedFailure() : undefined,
    boundaryResolved: false,
  };
}

/** Classify the latest visible failure for compact sidebar treatment. */
export function latestActivityErrorState(
  blocks: readonly AnyBlock[],
  hostOnline?: boolean | null,
): LatestSessionError | null | undefined {
  return latestActivityErrorWindow(blocks, hostOnline).state;
}

/** The latest visible activity, ignoring transcript bookkeeping. */
export function latestActivityIsError(blocks: readonly AnyBlock[]): boolean | undefined {
  const state = latestActivityErrorState(blocks);
  return state === undefined ? undefined : state === "error" || state === "disconnected";
}
