// Runtime companion to the `ConversationState` type: its key set and its
// initial values.
//
// Both are derived from the type via exhaustive records, so adding a field to
// `ConversationState` fails the build here rather than silently skipping the
// split or the reset. The key set is what `createConversationStore`'s setter
// uses to route a patch — a key missing from it would write conversation state
// onto the app-global store instead, where it would look correct until a second
// conversation existed.

import type { ConversationState } from "./chatStore";

// Exhaustive by construction: `Record<keyof ConversationState, true>` rejects a
// missing key and a stale one.
const CONVERSATION_STATE_KEY_MAP: Record<keyof ConversationState, true> = {
  blocks: true,
  pendingUserMessages: true,
  activeResponse: true,
  interruptedResponseIds: true,
  status: true,
  sessionStatus: true,
  backgroundTaskCount: true,
  blockedOn: true,
  isNativeTerminalSession: true,
  nativeVendorOwnsModel: true,
  boundAgentId: true,
  boundAgentName: true,
  loadingConversation: true,
  conversationLoadError: true,
  sessionModelOverride: true,
  sessionReasoningEffort: true,
  costControlModeOverride: true,
  subagentRoutingOverride: true,
  codexPlanMode: true,
  hasMoreHistory: true,
  loadingMoreHistory: true,
  oldestItemId: true,
  llmModel: true,
  sessionHarness: true,
  subAgentName: true,
  contextWindow: true,
  tokensUsed: true,
  sessionCostUsd: true,
  sessionUsageByModel: true,
  gitBranch: true,
  todos: true,
  skills: true,
  codexModelOptions: true,
  terminalPending: true,
  viewers: true,
  sandboxStatus: true,
  mcpStartup: true,
  abortController: true,
  runnerLaunchedAt: true,
  failedSendDraft: true,
  sendLatchedAt: true,
  historyGeneration: true,
};

const CONVERSATION_STATE_KEYS = new Set<string>(Object.keys(CONVERSATION_STATE_KEY_MAP));

/** Whether `key` names conversation-scoped state (vs. app-global or an action). */
export function isConversationStateKey(key: string | symbol): key is keyof ConversationState {
  return typeof key === "string" && CONVERSATION_STATE_KEYS.has(key);
}

/**
 * A conversation's state before anything is bound — what a cold load starts
 * from, and what `switchTo` used to reset the single active slot to.
 */
export function createInitialConversationState(): ConversationState {
  return {
    blocks: [],
    pendingUserMessages: [],
    activeResponse: null,
    interruptedResponseIds: [],
    status: "idle",
    sessionStatus: "idle",
    backgroundTaskCount: 0,
    blockedOn: null,
    isNativeTerminalSession: false,
    nativeVendorOwnsModel: false,
    boundAgentId: null,
    boundAgentName: null,
    loadingConversation: false,
    conversationLoadError: null,
    sessionModelOverride: null,
    sessionReasoningEffort: null,
    costControlModeOverride: null,
    subagentRoutingOverride: null,
    codexPlanMode: false,
    hasMoreHistory: false,
    loadingMoreHistory: false,
    oldestItemId: null,
    llmModel: null,
    sessionHarness: null,
    subAgentName: null,
    contextWindow: null,
    tokensUsed: null,
    sessionCostUsd: null,
    sessionUsageByModel: null,
    gitBranch: null,
    todos: [],
    skills: [],
    codexModelOptions: [],
    terminalPending: false,
    viewers: [],
    sandboxStatus: null,
    mcpStartup: null,
    abortController: null,
    runnerLaunchedAt: null,
    failedSendDraft: null,
    sendLatchedAt: null,
    historyGeneration: 0,
  };
}
