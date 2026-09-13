// Shared routing-decision test data. The same verdict shows up in three
// vocabularies — the persisted wire row, the block-stream block, and the chip /
// card component props — and the suites that cross those seams (renderItems,
// StatusBlocks, routingDecision) should agree on one shape each.

import type { AnyBlock, BlockContext } from "../blocks";
import type { ConversationItem } from "../conversationItems";

/** Snake_case fields of a persisted `routing_decision` transcript item. */
export interface RoutingDecisionWire {
  id?: string;
  response_id?: string;
  model?: string;
  applied?: boolean;
  rationale?: string;
  agent?: string;
  harness?: string;
  scope?: string;
  decision_id?: string;
  raw_model?: string;
  attempted_override?: string;
}

/**
 * A persisted `routing_decision` item as the reload funnel receives it.
 *
 * @param overrides - Wire fields to set or replace.
 * @returns A `ConversationItem`-shaped routing-decision row.
 */
export function routingDecisionItem(overrides: RoutingDecisionWire = {}): ConversationItem {
  return {
    id: "rd_1",
    type: "routing_decision",
    response_id: "routing_1",
    status: "completed",
    model: "databricks-claude-opus-4-8",
    applied: true,
    rationale: "multi-file refactor needs deep reasoning",
    ...overrides,
  } as unknown as ConversationItem;
}

/**
 * A `routing_decision` block as the live reducer emits it.
 *
 * @param ctx - Block context (item/response id) for this decision.
 * @param overrides - Verdict fields to set or replace.
 * @returns A `routing_decision` block.
 */
export function routingDecisionBlock(
  ctx: BlockContext,
  overrides: Record<string, unknown> = {},
): AnyBlock {
  return {
    type: "routing_decision",
    ctx,
    model: "databricks-claude-opus-4-8",
    applied: true,
    rationale: "hard turn",
    ...overrides,
  } as AnyBlock;
}
