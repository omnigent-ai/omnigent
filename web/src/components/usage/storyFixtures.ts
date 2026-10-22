import type { SessionUsage } from "@/lib/usageApi";

export const usageStoryNow = new Date("2026-03-10T12:00:00Z");

export const usageStorySessions: SessionUsage[] = [
  {
    id: "conversation-beta",
    createdAt: 1_773_057_600,
    updatedAt: 1_772_971_200,
    title: "Daily log summarizer",
    costUsd: 128.9,
    models: { "gpt-5.3-codex": 128.9 },
    harness: "codex",
    otherHarnesses: ["opencode", "gemini-cli"],
    llmModel: null,
    agentName: null,
  },
  {
    id: "conversation-alpha",
    createdAt: 1_773_057_600,
    updatedAt: 1_773_143_970,
    title: "Refactor auth middleware",
    costUsd: 12.34,
    models: { "claude-opus-4-8": 12.34 },
    harness: "claude-code",
    otherHarnesses: null,
    llmModel: null,
    agentName: null,
  },
  {
    id: "conversation-gamma",
    createdAt: 1_773_057_600,
    updatedAt: 1_773_143_700,
    title: "Migrate billing webhooks",
    costUsd: 7.5,
    models: {
      "claude-opus-4-8": 5,
      "claude-sonnet-5": 2,
      "claude-haiku-4-5": 0.5,
    },
    harness: "claude-code",
    otherHarnesses: null,
    llmModel: null,
    agentName: null,
  },
  {
    id: "conversation-delta",
    createdAt: 1_773_057_600,
    updatedAt: 1_773_133_200,
    title: null,
    costUsd: 0.004,
    models: {},
    harness: null,
    otherHarnesses: null,
    llmModel: null,
    agentName: null,
  },
];
