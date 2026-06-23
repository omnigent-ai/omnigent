import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  type ActiveChatOverride,
  computeNextActiveOverride,
  conversationDisplayLabel,
  filterConversations,
  getConversationIconKind,
  getConversationAgentType,
  normalizePinnedConversationIds,
  orderByPinnedSequence,
  togglePinnedConversationId,
} from "./sidebarNav";

function conversation(
  id: string,
  title: string | null,
  createdAt: Date,
  options: { labels?: Record<string, string>; updatedAt?: Date; archived?: boolean } = {},
): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: Math.floor(createdAt.getTime() / 1000),
    updated_at: Math.floor((options.updatedAt ?? createdAt).getTime() / 1000),
    labels: options.labels ?? {},
    permission_level: null,
    archived: options.archived,
  };
}

describe("filterConversations", () => {
  it("matches title and id case-insensitively", () => {
    const conversations = [
      conversation("conv_alpha", "Weather Notes", new Date(2026, 4, 14, 10)),
      conversation("conv_beta", "Build UI", new Date(2026, 4, 14, 9)),
      conversation("conv_gamma", null, new Date(2026, 4, 14, 8)),
    ];

    expect(filterConversations(conversations, " weather ").map((c) => c.id)).toEqual([
      "conv_alpha",
    ]);
    expect(filterConversations(conversations, "GAMMA").map((c) => c.id)).toEqual(["conv_gamma"]);
  });

  it("matches native wrapper default labels for untitled sessions", () => {
    const conversations = [
      conversation("conv_native", null, new Date(2026, 4, 14, 9), {
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
      conversation("conv_codex", null, new Date(2026, 4, 14, 8), {
        labels: { "omnigent.wrapper": "codex-native-ui" },
      }),
      conversation("conv_pi", null, new Date(2026, 4, 14, 8), {
        labels: { "omnigent.wrapper": "pi-native-ui" },
      }),
      conversation("conv_other", null, new Date(2026, 4, 14, 8)),
    ];

    expect(filterConversations(conversations, "claude").map((c) => c.id)).toEqual(["conv_native"]);
    expect(filterConversations(conversations, "codex").map((c) => c.id)).toEqual(["conv_codex"]);
    expect(filterConversations(conversations, "pi").map((c) => c.id)).toEqual(["conv_pi"]);
  });
});

describe("computeNextActiveOverride", () => {
  const a = conversation("a", "A", new Date(2026, 4, 14, 9));
  const b = conversation("b", "B", new Date(2026, 4, 14, 10));

  it("returns null when no chat is active", () => {
    expect(computeNextActiveOverride(undefined, [a, b], null)).toBeNull();
  });

  it("snaps to the active chat's current updated_at on first observation", () => {
    expect(computeNextActiveOverride("a", [a, b], null)).toEqual({
      id: "a",
      updatedAt: a.updated_at,
    });
  });

  it("keeps the existing snapshot when activeId hasn't changed", () => {
    // The whole point of the freeze: a server-side updated_at bump on
    // the active chat must not refresh the snapshot.
    const frozen: ActiveChatOverride = { id: "a", updatedAt: 100 };
    const aBumped = { ...a, updated_at: 999 };
    expect(computeNextActiveOverride("a", [aBumped, b], frozen)).toBe(frozen);
  });

  it("snaps to the new active chat when navigating between chats", () => {
    expect(computeNextActiveOverride("b", [a, b], { id: "a", updatedAt: 100 })).toEqual({
      id: "b",
      updatedAt: b.updated_at,
    });
  });

  it("drops the override while the new active chat hasn't loaded yet", () => {
    expect(computeNextActiveOverride("c", [a, b], { id: "a", updatedAt: 100 })).toBeNull();
  });
});

describe("pin helpers", () => {
  it("toggles pinned ids without duplicating them", () => {
    expect(togglePinnedConversationId(["conv_a"], "conv_b")).toEqual(["conv_b", "conv_a"]);
    expect(togglePinnedConversationId(["conv_b", "conv_a"], "conv_b")).toEqual(["conv_a"]);
  });

  it("drops stale and duplicate pinned ids", () => {
    const conversations = [
      conversation("conv_a", "A", new Date(2026, 4, 14, 9)),
      conversation("conv_b", "B", new Date(2026, 4, 14, 8)),
    ];

    expect(
      normalizePinnedConversationIds(["conv_a", "missing", "conv_a", "conv_b"], conversations),
    ).toEqual(["conv_a", "conv_b"]);
  });
});

describe("orderByPinnedSequence", () => {
  it("puts the newest pin last, ignoring updated_at", () => {
    // conv_a leads the ids list (the most recent pin) AND has the newest
    // updated_at, yet it must render LAST: pinned order is oldest-pin-first
    // (newest pin at the bottom) and never follows updated_at.
    const convA = conversation("conv_a", "A", new Date(2026, 4, 14, 9), {
      updatedAt: new Date(2026, 4, 14, 23),
    });
    const convB = conversation("conv_b", "B", new Date(2026, 4, 14, 8), {
      updatedAt: new Date(2026, 4, 14, 9),
    });

    // ids are most-recently-pinned-first: conv_a pinned last, conv_b earlier.
    expect(orderByPinnedSequence([convA, convB], ["conv_a", "conv_b"]).map((c) => c.id)).toEqual([
      "conv_b",
      "conv_a",
    ]);
  });

  it("holds a pinned row's slot when its updated_at is bumped", () => {
    const convA = conversation("conv_a", "A", new Date(2026, 4, 14, 9));
    const convB = conversation("conv_b", "B", new Date(2026, 4, 14, 8));
    const ids = ["conv_a", "conv_b"];

    const before = orderByPinnedSequence([convA, convB], ids).map((c) => c.id);
    // conv_b gets a new message (latest updated_at) — its slot must not move.
    const bumped = { ...convB, updated_at: Math.floor(new Date(2026, 4, 14, 23).getTime() / 1000) };
    const after = orderByPinnedSequence([convA, bumped], ids).map((c) => c.id);
    expect(after).toEqual(before);
  });

  it("does not mutate the input array", () => {
    const convA = conversation("conv_a", "A", new Date(2026, 4, 14, 9));
    const convB = conversation("conv_b", "B", new Date(2026, 4, 14, 8));
    const input = [convB, convA];
    orderByPinnedSequence(input, ["conv_a", "conv_b"]);
    expect(input.map((c) => c.id)).toEqual(["conv_b", "conv_a"]);
  });
});

describe("getConversationAgentType", () => {
  it("returns 'Claude Code' for claude-native-ui sessions", () => {
    const conv = conversation("conv_native", null, new Date(2026, 4, 14, 9), {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
    });
    // claude-code-native-ui is the wrapper label assigned to sessions started
    // via `omnigent claude`. Any other label value must not match.
    expect(getConversationAgentType(conv)).toBe("Claude Code");
  });

  it("returns 'Codex' for codex-native-ui sessions", () => {
    const conv = conversation("conv_codex", null, new Date(2026, 4, 14, 9), {
      labels: { "omnigent.wrapper": "codex-native-ui" },
    });
    // codex-native-ui is the wrapper label assigned to sessions started
    // via `omnigent codex`. It gets its own filter bucket and row icon.
    expect(getConversationAgentType(conv)).toBe("Codex");
  });

  it("returns 'Pi' for pi-native-ui sessions", () => {
    const conv = conversation("conv_pi", null, new Date(2026, 4, 14, 9), {
      labels: { "omnigent.wrapper": "pi-native-ui" },
    });
    expect(getConversationAgentType(conv)).toBe("Pi");
  });

  it("returns agent_name for YAML-based sessions", () => {
    const conv: Conversation = {
      ...conversation("conv_yaml", "My session", new Date(2026, 4, 14, 9)),
      agent_name: "databricks_coding_agent",
    };
    // agent_name comes from the agent spec's `name:` field; it's the canonical
    // identity for YAML-based agents and is preferred over the id.
    expect(getConversationAgentType(conv)).toBe("databricks_coding_agent");
  });

  it("returns 'Other' when no wrapper label and no agent_name", () => {
    const conv = conversation("conv_plain", "Some chat", new Date(2026, 4, 14, 9));
    // Sessions with no wrapper and no agent_name are unclassified; 'Other'
    // is the catch-all bucket in the filter dropdown.
    expect(getConversationAgentType(conv)).toBe("Other");
  });

  it("prefers native wrapper labels over agent_name when both are set", () => {
    // In practice the native wrapper never sets agent_name, but if it did the
    // wrapper label wins so the filter bucket stays consistent with the row icon.
    const claudeConv: Conversation = {
      ...conversation("conv_both", null, new Date(2026, 4, 14, 9), {
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
      }),
      agent_name: "some_agent",
    };
    const codexConv: Conversation = {
      ...conversation("conv_both_codex", null, new Date(2026, 4, 14, 9), {
        labels: { "omnigent.wrapper": "codex-native-ui" },
      }),
      agent_name: "some_agent",
    };
    const piConv: Conversation = {
      ...conversation("conv_both_pi", null, new Date(2026, 4, 14, 9), {
        labels: { "omnigent.wrapper": "pi-native-ui" },
      }),
      agent_name: "some_agent",
    };
    expect(getConversationAgentType(claudeConv)).toBe("Claude Code");
    expect(getConversationAgentType(codexConv)).toBe("Codex");
    expect(getConversationAgentType(piConv)).toBe("Pi");
  });

  it("returns 'Other' when agent_name is null", () => {
    const conv: Conversation = {
      ...conversation("conv_null_name", "Chat", new Date(2026, 4, 14, 9)),
      agent_name: null,
    };
    // Explicit null is equivalent to absent — do not render null as the type name.
    expect(getConversationAgentType(conv)).toBe("Other");
  });
});

describe("getConversationIconKind", () => {
  it("maps native wrapper labels and nessie agent names to row icon kinds", () => {
    expect(
      getConversationIconKind(
        conversation("conv_claude", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        }),
      ),
    ).toBe("claude");
    expect(
      getConversationIconKind(
        conversation("conv_codex", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "codex-native-ui" },
        }),
      ),
    ).toBe("codex");
    expect(
      getConversationIconKind(
        conversation("conv_pi", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "pi-native-ui" },
        }),
      ),
    ).toBe("pi");
    expect(
      getConversationIconKind({
        ...conversation("conv_nessie", null, new Date(2026, 4, 14, 9)),
        agent_name: "nessie",
      }),
    ).toBe("nessie");
    expect(
      getConversationIconKind(conversation("conv_other", null, new Date(2026, 4, 14, 9))),
    ).toBeNull();
  });
});

describe("conversationDisplayLabel", () => {
  it("uses the title when present and a 'New session' fallback otherwise", () => {
    expect(
      conversationDisplayLabel(
        conversation("conv_abcdefghijklmnopqrstuvwxyz", "Named chat", new Date(2026, 4, 14, 9)),
      ),
    ).toBe("Named chat");
    expect(
      conversationDisplayLabel(
        conversation("conv_abcdefghijklmnopqrstuvwxyz", null, new Date(2026, 4, 14, 9)),
      ),
    ).toBe("New session");
  });

  it("falls back to 'Claude Code' for claude-native sessions with no title", () => {
    expect(
      conversationDisplayLabel(
        conversation("conv_abcdefghijklmnopqrstuvwxyz", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        }),
      ),
    ).toBe("Claude Code");
  });

  it("falls back to 'Codex' for codex-native sessions with no title", () => {
    expect(
      conversationDisplayLabel(
        conversation("conv_abcdefghijklmnopqrstuvwxyz", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "codex-native-ui" },
        }),
      ),
    ).toBe("Codex");
  });

  it("falls back to 'Pi' for pi-native sessions with no title", () => {
    expect(
      conversationDisplayLabel(
        conversation("conv_abcdefghijklmnopqrstuvwxyz", null, new Date(2026, 4, 14, 9), {
          labels: { "omnigent.wrapper": "pi-native-ui" },
        }),
      ),
    ).toBe("Pi");
  });

  it("prefers the actual title over the claude-native fallback once set", () => {
    expect(
      conversationDisplayLabel(
        conversation(
          "conv_abcdefghijklmnopqrstuvwxyz",
          "investigate the regression",
          new Date(2026, 4, 14, 9),
          { labels: { "omnigent.wrapper": "claude-code-native-ui" } },
        ),
      ),
    ).toBe("investigate the regression");
  });
});
