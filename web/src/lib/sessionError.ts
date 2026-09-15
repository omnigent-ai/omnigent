import type { AnyBlock } from "./blocks";

/** The latest visible activity, ignoring transcript bookkeeping. */
export function latestActivityIsError(blocks: readonly AnyBlock[]): boolean | undefined {
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    switch (block.type) {
      case "error":
        return block.level !== "info";
      case "text_done":
        // Claude's native transcript can persist an API rejection as ordinary
        // assistant text, without an error item or a failed session status.
        return /^API Error:\s*\S/.test(block.fullText.trimStart());
      case "response_end":
        if (block.status === "failed") return true;
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
        return false;
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
  return undefined;
}
