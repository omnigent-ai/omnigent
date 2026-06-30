import { describe, expect, it, vi } from "vitest";
import type { RenderItem } from "@/lib/renderItems";
import type { ToolExecution } from "@/lib/blocks";
import type { Bubble } from "@/lib/renderItems";
import { BUILTIN_SLASH_COMMANDS, isSlashCommandText } from "@/components/SlashCommandMenu";
import { isSessionSharedWithOthers } from "@/lib/permissionsApi";
import {
  buildPendingBubbles,
  buildSlashCommandMap,
  buildSlashCommandWithArgsSet,
  collectBubbleMarkdown,
  collectPendingElicitations,
  computeIsWorking,
  computeShowsWorking,
  containsMarkdownTable,
  dispatchInitialPrompt,
  isUnboundCodingFork,
  mergePendingBubbles,
  readOnlyReasonForSessionLabels,
  reorderCommittedRequestElicitations,
  shouldSendInitialPrompt,
  shouldShowAuthorBadge,
  shouldShowWorkingIndicator,
  shouldShowTerminalSurface,
  splitSlashCommand,
  stripPendingElicitations,
  subAgentComposerLabel,
  workingIndicatorLabel,
} from "./ChatPage";

// The Composer's read-only and disabled states are derived from
// permissionLevel. These tests pin the derivation logic so a
// refactor can't accidentally let read-only users send messages
// or hide the share button from managers.

function composerState(permissionLevel: number | null) {
  const isReadOnly = permissionLevel === 1;
  const canType = !isReadOnly;
  const canSubmit = !isReadOnly;
  return { isReadOnly, canType, canSubmit };
}

describe("Composer permission gating", () => {
  it("read-only (level 1) disables textarea and submit", () => {
    const state = composerState(1);
    expect(state.isReadOnly).toBe(true);
    expect(state.canType).toBe(false);
    expect(state.canSubmit).toBe(false);
  });

  it("edit (level 2) allows typing and submitting", () => {
    const state = composerState(2);
    expect(state.isReadOnly).toBe(false);
    expect(state.canType).toBe(true);
    expect(state.canSubmit).toBe(true);
  });

  it("manage (level 3) allows typing and submitting", () => {
    const state = composerState(3);
    expect(state.isReadOnly).toBe(false);
    expect(state.canType).toBe(true);
  });

  it("owner (level 4) allows typing and submitting", () => {
    const state = composerState(4);
    expect(state.isReadOnly).toBe(false);
    expect(state.canType).toBe(true);
  });

  it("null permission (no auth) allows typing and submitting", () => {
    const state = composerState(null);
    expect(state.isReadOnly).toBe(false);
    expect(state.canType).toBe(true);
  });
});

describe("Composer structural read-only reasons", () => {
  it("uses the closed-session reason when the live snapshot is closed", () => {
    expect(
      readOnlyReasonForSessionLabels(
        { labels: { "omnigent.closed": "true" } },
        { labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" } },
      ),
    ).toBe("This sub-agent session is closed");
  });

  it("falls back to the sidebar wrapper label for native read-only children", () => {
    expect(
      readOnlyReasonForSessionLabels(null, {
        labels: { "omnigent.wrapper": "claude-code-native-ui-subagent" },
      }),
    ).toBe("Claude Code sub-agents are read-only");
  });

  it("returns null for editable sessions without structural labels", () => {
    expect(readOnlyReasonForSessionLabels({ labels: {} }, { labels: {} })).toBeNull();
  });
});

describe("Terminal-first surface selection", () => {
  it("keeps Terminal view selected even when the runner is offline", () => {
    expect(
      shouldShowTerminalSurface("conv_stopped", { isTerminalFirst: true, view: "terminal" }, false),
    ).toBe(true);
  });

  it("does not show Terminal view for chat mode or non-terminal-first sessions", () => {
    expect(
      shouldShowTerminalSurface("conv_terminal", { isTerminalFirst: true, view: "chat" }, true),
    ).toBe(false);
    expect(
      shouldShowTerminalSurface("conv_regular", { isTerminalFirst: false, view: "terminal" }, true),
    ).toBe(false);
  });
});

// Share button visibility logic (mirrored from AppShell)
function shareButtonState(permissionLevel: number | null) {
  const canManage = permissionLevel === null || permissionLevel >= 3;
  return { canManage };
}

describe("Share button permission gating", () => {
  it("enabled for manage (level 3)", () => {
    expect(shareButtonState(3).canManage).toBe(true);
  });

  it("enabled for owner (level 4)", () => {
    expect(shareButtonState(4).canManage).toBe(true);
  });

  it("disabled for edit (level 2)", () => {
    expect(shareButtonState(2).canManage).toBe(false);
  });

  it("disabled for read (level 1)", () => {
    expect(shareButtonState(1).canManage).toBe(false);
  });

  it("enabled when null (no auth)", () => {
    expect(shareButtonState(null).canManage).toBe(true);
  });
});

interface ActiveTurnComposerStateInput {
  isWorking: boolean;
  value?: string;
  fileCount?: number;
  disabled?: boolean;
  permissionLevel?: number | null;
}

// Mirrors the Composer button state: an active turn with no draft becomes
// an interrupt button, but typed text or attachments keep the submit path
// available so follow-ups can be queued.
function activeTurnComposerState({
  isWorking,
  value = "",
  fileCount = 0,
  disabled = false,
  permissionLevel = null,
}: ActiveTurnComposerStateInput) {
  const isReadOnly = permissionLevel === 1;
  const hasDraft = value.trim().length > 0 || fileCount > 0;
  const showInterruptButton = isWorking && !hasDraft;
  const submitDisabled = showInterruptButton ? isReadOnly : !hasDraft || disabled || isReadOnly;
  return {
    hasDraft,
    showInterruptButton,
    submitDisabled,
    ariaLabel: showInterruptButton ? "Interrupt" : "Send",
  };
}

describe("Composer active-turn controls", () => {
  it("shows an enabled interrupt button while working with an empty draft", () => {
    expect(activeTurnComposerState({ isWorking: true })).toEqual({
      hasDraft: false,
      showInterruptButton: true,
      submitDisabled: false,
      ariaLabel: "Interrupt",
    });
  });

  it("keeps send mode available for queued follow-ups while working", () => {
    expect(activeTurnComposerState({ isWorking: true, value: "follow up" })).toMatchObject({
      hasDraft: true,
      showInterruptButton: false,
      submitDisabled: false,
      ariaLabel: "Send",
    });
  });

  it("keeps send mode available for file-only follow-ups while working", () => {
    expect(activeTurnComposerState({ isWorking: true, fileCount: 1 })).toMatchObject({
      hasDraft: true,
      showInterruptButton: false,
      submitDisabled: false,
      ariaLabel: "Send",
    });
  });

  it("does not let read-only users interrupt or send", () => {
    expect(activeTurnComposerState({ isWorking: true, permissionLevel: 1 })).toMatchObject({
      showInterruptButton: true,
      submitDisabled: true,
    });
    expect(
      activeTurnComposerState({ isWorking: true, value: "follow up", permissionLevel: 1 }),
    ).toMatchObject({
      showInterruptButton: false,
      submitDisabled: true,
    });
  });
});

// ── shouldShowAuthorBadge ──────────────────────────────────────────────────

describe("shouldShowAuthorBadge", () => {
  it("badges another contributor's message in a shared session", () => {
    expect(shouldShowAuthorBadge("bob@example.com", "alice@example.com", true)).toBe(true);
  });

  it("never badges the viewer's own message", () => {
    // The UX guidance: you know what you sent — avatars mark OTHER
    // contributors only. A true here would put your own circle on
    // every message you write in a shared session.
    expect(shouldShowAuthorBadge("alice@example.com", "alice@example.com", true)).toBe(false);
  });

  it("hides the badge when no author is attached", () => {
    // Agent/tool/system output and pre-attribution history both leave
    // createdBy undefined; neither should carry a human author badge.
    expect(shouldShowAuthorBadge(undefined, "alice@example.com", true)).toBe(false);
  });

  it("hides the badge in an unshared session", () => {
    // Solo sessions have no co-viewers to attribute messages to, even
    // when createdBy is stamped.
    expect(shouldShowAuthorBadge("bob@example.com", "alice@example.com", false)).toBe(false);
  });

  it("badges foreign authors even when the viewer has no identity", () => {
    // A null viewer (identity not yet resolved) still sees other
    // contributors' badges in a shared session — author !== null.
    expect(shouldShowAuthorBadge("bob@example.com", null, true)).toBe(true);
  });
});

// ── isSessionSharedWithOthers ───────────────────────────────────────────────

describe("isSessionSharedWithOthers", () => {
  it("is shared when another principal owns the session (shared with the viewer)", () => {
    expect(isSessionSharedWithOthers("alice@example.com", "bob@example.com", undefined)).toBe(true);
  });

  it("is private for a solo session the viewer owns with only their own grant", () => {
    expect(
      isSessionSharedWithOthers("alice@example.com", "alice@example.com", [
        { user_id: "alice@example.com" },
      ]),
    ).toBe(false);
  });

  it("is shared once the owner grants another user", () => {
    expect(
      isSessionSharedWithOthers("alice@example.com", "alice@example.com", [
        { user_id: "alice@example.com" },
        { user_id: "bob@example.com" },
      ]),
    ).toBe(true);
  });

  it("treats a public grant as shared", () => {
    expect(
      isSessionSharedWithOthers("alice@example.com", "alice@example.com", [
        { user_id: "__public__" },
      ]),
    ).toBe(true);
  });

  it("is private while the owner's grant list is still loading", () => {
    // undefined grants (query disabled / in-flight) must not flash labels on.
    expect(isSessionSharedWithOthers("alice@example.com", "alice@example.com", undefined)).toBe(
      false,
    );
  });

  it("is private in single-user mode (no owner, no viewer)", () => {
    expect(isSessionSharedWithOthers(null, null, undefined)).toBe(false);
  });
});

// ── buildPendingBubbles ────────────────────────────────────────────────────

describe("buildPendingBubbles", () => {
  const pending = [{ tempId: "tmp_1", content: [{ type: "input_text" as const, text: "hello" }] }];

  it("stamps the viewer's own author so the optimistic bubble is labeled live", () => {
    // The own-message parity fix: a pending send shows the viewer's name
    // immediately, instead of staying blank until session.input.consumed
    // promotes it.
    const [bubble] = buildPendingBubbles(pending, "alice@example.com") as [
      Extract<Bubble, { kind: "user" }>,
    ];
    expect(bubble.kind).toBe("user");
    expect(bubble.itemId).toBe("tmp_1");
    expect(bubble.content).toEqual([{ type: "input_text", text: "hello" }]);
    expect(bubble.createdBy).toBe("alice@example.com");
  });

  it("leaves the bubble unlabeled when self-author is null (local / unresolved)", () => {
    // getCurrentAuthorId() returns null in single-user mode and before
    // identity resolves; the optimistic bubble must not carry an author.
    const [bubble] = buildPendingBubbles(pending, null) as [Extract<Bubble, { kind: "user" }>];
    expect(bubble.createdBy).toBeUndefined();
  });

  it("prefers p.author over the viewer selfAuthor for snapshot-replayed entries", () => {
    // A collaborator reconnecting to a shared session re-hydrates pending
    // entries from the server snapshot. Those entries carry the original
    // sender's email in p.author. buildPendingBubbles must use that
    // value instead of the reconnecting viewer's identity — otherwise the
    // bubble would show Alice's email on a message that Bob sent.
    const replayedPending = [
      {
        tempId: "tmp_snap",
        content: [{ type: "input_text" as const, text: "hi" }],
        author: "bob@example.com",
      },
    ];
    const [bubble] = buildPendingBubbles(replayedPending, "alice@example.com") as [
      Extract<Bubble, { kind: "user" }>,
    ];
    // p.author ("bob") wins over the viewer's selfAuthor ("alice").
    expect(bubble.createdBy).toBe("bob@example.com");
  });
});

// ── mergePendingBubbles ────────────────────────────────────────────────────

// Shared bubble builders for the request-elicitation ordering tests.
const userBubble = (id: string): Bubble => ({
  kind: "user",
  itemId: id,
  content: [{ type: "input_text", text: id }],
});
const assistantText = (id: string): Bubble => ({
  kind: "assistant",
  responseId: id,
  stableId: id,
  lifecycle: "completed",
  error: null,
  items: [{ kind: "text", itemId: id, text: "hi", final: true }],
});
const elicitationBubble = (id: string, phase: string): Bubble => ({
  kind: "assistant",
  responseId: id,
  stableId: id,
  lifecycle: "completed",
  error: null,
  items: [
    {
      kind: "elicitation",
      itemId: id,
      elicitationId: id,
      message: "Continue?",
      phase,
      policyName: "session_cost_budget",
      contentPreview: "{}",
      requestedSchema: {},
      status: "pending",
      response: null,
    },
  ],
});
const bubbleIds = (bubbles: Bubble[]): string[] =>
  bubbles.map((b) => (b.kind === "user" ? b.itemId : b.kind === "assistant" ? b.stableId : ""));

describe("mergePendingBubbles", () => {
  it("appends pending bubbles at the end when nothing trails", () => {
    const committed = [userBubble("u1"), assistantText("a1")];
    const pending = [userBubble("pend_1")];
    const merged = mergePendingBubbles(committed, pending);
    expect(merged.map((b) => (b.kind === "assistant" ? b.stableId : b.itemId))).toEqual([
      "u1",
      "a1",
      "pend_1",
    ]);
  });

  it("returns committed unchanged when there are no pending bubbles", () => {
    const committed = [userBubble("u1"), elicitationBubble("e1", "request")];
    const merged = mergePendingBubbles(committed, []);
    expect(merged).toBe(committed);
  });

  it("splices the pending prompt ABOVE a trailing request-phase elicitation card", () => {
    // The bug: a REQUEST-phase ASK parks the user message server-side, so
    // it stays an optimistic pending bubble while its card arrives as a
    // committed bubble — appending after the card would show the approval
    // prompt above the message that triggered it.
    const committed = [assistantText("a1"), elicitationBubble("e1", "request")];
    const pending = [userBubble("pend_1")];
    const merged = mergePendingBubbles(committed, pending);
    expect(merged.map((b) => (b.kind === "assistant" ? b.stableId : b.itemId))).toEqual([
      "a1",
      "pend_1",
      "e1",
    ]);
  });

  it("splices above a run of multiple trailing request-phase elicitations", () => {
    const committed = [elicitationBubble("e1", "request"), elicitationBubble("e2", "request")];
    const pending = [userBubble("pend_1")];
    const merged = mergePendingBubbles(committed, pending);
    expect(merged.map((b) => (b.kind === "assistant" ? b.stableId : b.itemId))).toEqual([
      "pend_1",
      "e1",
      "e2",
    ]);
  });

  it("does NOT reorder for a tool_call-phase elicitation (message already committed)", () => {
    // A tool_call ASK fires after the user message is committed into the
    // timeline, so the trailing append is correct — only request-phase
    // cards need the prompt lifted above them.
    const committed = [userBubble("u1"), elicitationBubble("e1", "tool_call")];
    const pending = [userBubble("pend_1")];
    const merged = mergePendingBubbles(committed, pending);
    expect(merged.map((b) => (b.kind === "assistant" ? b.stableId : b.itemId))).toEqual([
      "u1",
      "e1",
      "pend_1",
    ]);
  });
});

// ── reorderCommittedRequestElicitations ─────────────────────────────────────

describe("reorderCommittedRequestElicitations", () => {
  it("swaps an approved request card below the user message it gated", () => {
    // After approval the parked message is consumed into `blocks` AFTER
    // the card, giving [card, message]. The card must drop below the
    // prompt that triggered it.
    const committed = [elicitationBubble("e1", "request"), userBubble("u1")];
    expect(bubbleIds(reorderCommittedRequestElicitations(committed))).toEqual(["u1", "e1"]);
  });

  it("swaps a cursor-native pre_tool_use card below the user message it gated", () => {
    // cursor-native's standalone approval card arrives in the live stream
    // before the forwarder mirrors the triggering user message, giving
    // [card, message]. It must drop below the prompt, exactly like REQUEST.
    const committed = [elicitationBubble("e1", "pre_tool_use"), userBubble("u1")];
    expect(bubbleIds(reorderCommittedRequestElicitations(committed))).toEqual(["u1", "e1"]);
  });

  it("keeps the card between the prompt and the assistant response", () => {
    const committed = [
      assistantText("a0"),
      elicitationBubble("e1", "request"),
      userBubble("u1"),
      assistantText("a1"),
    ];
    expect(bubbleIds(reorderCommittedRequestElicitations(committed))).toEqual([
      "a0",
      "u1",
      "e1",
      "a1",
    ]);
  });

  it("leaves a lone request card (declined / still pending) untouched and returns same ref", () => {
    const committed = [assistantText("a0"), elicitationBubble("e1", "request")];
    const result = reorderCommittedRequestElicitations(committed);
    expect(result).toBe(committed);
  });

  it("does NOT reorder a tool_call-phase card followed by a user message", () => {
    const committed = [elicitationBubble("e1", "tool_call"), userBubble("u1")];
    const result = reorderCommittedRequestElicitations(committed);
    expect(result).toBe(committed);
    expect(bubbleIds(result)).toEqual(["e1", "u1"]);
  });
});

// ── pinned elicitations ─────────────────────────────────────────────────────

// Assistant bubble carrying arbitrary render items — lets these tests mix a
// card with trailing text (the "card + a bunch of text" case that motivated
// pinning) and toggle a card's answered status.
const assistantWith = (id: string, items: RenderItem[]): Bubble => ({
  kind: "assistant",
  responseId: id,
  stableId: id,
  lifecycle: "completed",
  error: null,
  items,
});
const textItem = (id: string): RenderItem => ({
  kind: "text",
  itemId: id,
  text: "hi",
  final: true,
});
const elicitItem = (id: string, status: "pending" | "responded"): RenderItem => ({
  kind: "elicitation",
  itemId: id,
  elicitationId: id,
  message: "Continue?",
  phase: "tool_call",
  policyName: "p",
  contentPreview: "{}",
  requestedSchema: {},
  status,
  response: status === "responded" ? { action: "accept" } : null,
});
const pendingIds = (items: RenderItem[]): string[] =>
  items.map((it) => (it.kind === "elicitation" ? it.elicitationId : ""));

describe("collectPendingElicitations", () => {
  it("collects pending cards across bubbles in document order (newest last)", () => {
    const bubbles = [
      assistantWith("a1", [elicitItem("e1", "pending")]),
      userBubble("u1"),
      assistantWith("a2", [textItem("t1"), elicitItem("e2", "pending")]),
    ];
    expect(pendingIds(collectPendingElicitations(bubbles))).toEqual(["e1", "e2"]);
  });

  it("ignores answered cards and user bubbles", () => {
    const bubbles = [
      userBubble("u1"),
      assistantWith("a1", [elicitItem("e1", "responded")]),
      assistantWith("a2", [elicitItem("e2", "pending")]),
    ];
    expect(pendingIds(collectPendingElicitations(bubbles))).toEqual(["e2"]);
  });

  it("returns an empty list when nothing is pending", () => {
    const bubbles = [userBubble("u1"), assistantText("a1")];
    expect(collectPendingElicitations(bubbles)).toEqual([]);
  });
});

describe("stripPendingElicitations", () => {
  it("removes a pending card but keeps the trailing text in the same bubble", () => {
    // The motivating case: the agent emits a card AND a bunch of text. The
    // card floats to the bottom (removed here); the text stays inline.
    const bubbles = [assistantWith("a1", [elicitItem("e1", "pending"), textItem("t1")])];
    const stripped = stripPendingElicitations(bubbles);
    const a1 = stripped[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a1.items.map((it) => it.kind)).toEqual(["text"]);
  });

  it("leaves answered cards inline", () => {
    const bubbles = [assistantWith("a1", [elicitItem("e1", "responded")])];
    const stripped = stripPendingElicitations(bubbles);
    expect(stripped).toBe(bubbles);
  });

  it("returns the same array reference when nothing is pending (memo stability)", () => {
    const bubbles = [userBubble("u1"), assistantText("a1")];
    expect(stripPendingElicitations(bubbles)).toBe(bubbles);
  });

  it("clones only the affected bubble and keeps other references intact", () => {
    const a1 = assistantText("a1");
    const a2 = assistantWith("a2", [elicitItem("e2", "pending")]);
    const stripped = stripPendingElicitations([a1, a2]);
    expect(stripped[0]).toBe(a1);
    expect(stripped[1]).not.toBe(a2);
    expect((stripped[1] as Extract<Bubble, { kind: "assistant" }>).items).toEqual([]);
  });
});

// ── computeIsWorking ───────────────────────────────────────────────────────

describe("computeIsWorking", () => {
  // The shimmer tracks the server session status 1:1 with the badge — there
  // are no optimistic inputs (no outstandingSends / pendingCount bridges).
  it("idle → false", () => {
    expect(computeIsWorking("idle")).toBe(false);
  });

  it("server says running → true", () => {
    expect(computeIsWorking("running")).toBe(true);
  });

  it("server says waiting (parked on async work) → true", () => {
    expect(computeIsWorking("waiting")).toBe(true);
  });

  it("failed → false", () => {
    expect(computeIsWorking("failed")).toBe(false);
  });
});

// ── computeShowsWorking (main chat display) ─────────────────────────────────

describe("computeShowsWorking", () => {
  const opts = (
    overrides: Partial<Parameters<typeof computeShowsWorking>[1]> = {},
  ): Parameters<typeof computeShowsWorking>[1] => ({
    hasPendingElicitation: false,
    runnerOnline: true,
    ...overrides,
  });

  it("parent idle stays idle in the main chat", () => {
    // Busy children are surfaced by the sidebar/Agents rail, not by the
    // main chat's shimmer or pinned "Working…" pill.
    expect(computeShowsWorking("idle", opts())).toBe(false);
  });

  it("parent running → working", () => {
    expect(computeShowsWorking("running", opts())).toBe(true);
  });

  it("parent waiting (parked on async) → working", () => {
    expect(computeShowsWorking("waiting", opts())).toBe(true);
  });

  it("parent failed → idle", () => {
    expect(computeShowsWorking("failed", opts())).toBe(false);
  });

  it("a pending elicitation suppresses the indicator", () => {
    // The elicitation prompt owns the in-progress slot — same suppression
    // the parent's own shimmer already applies.
    expect(computeShowsWorking("running", opts({ hasPendingElicitation: true }))).toBe(false);
  });

  it("a known-offline runner suppresses stale working status", () => {
    expect(computeShowsWorking("running", opts({ runnerOnline: false }))).toBe(false);
    expect(computeShowsWorking("waiting", opts({ runnerOnline: false }))).toBe(false);
  });

  it("unresolved runner health does not suppress active parent work", () => {
    expect(computeShowsWorking("running", opts({ runnerOnline: undefined }))).toBe(true);
    expect(computeShowsWorking("waiting", opts({ runnerOnline: undefined }))).toBe(true);
  });

  it("background shells keep the indicator lit after the turn settles to idle", () => {
    // A claude-native turn ends at `idle` (the PTY-activity watcher's edge)
    // even while background shells run. The shell count keeps the indicator
    // visible so "N background tasks still running" stays on screen.
    expect(computeShowsWorking("idle", opts({ backgroundTaskCount: 1 }))).toBe(true);
  });

  it("a zero shell count at idle stays idle", () => {
    expect(computeShowsWorking("idle", opts({ backgroundTaskCount: 0 }))).toBe(false);
  });

  it("a known-offline runner suppresses the indicator even with background shells", () => {
    // Offline beats every other signal: a stale shell count must not keep a
    // dead session spinning.
    expect(computeShowsWorking("idle", opts({ runnerOnline: false, backgroundTaskCount: 2 }))).toBe(
      false,
    );
  });
});

// ── shouldShowWorkingIndicator ──────────────────────────────────────────────

describe("shouldShowWorkingIndicator", () => {
  it("shows Working for a running main session with no hydrated bubbles", () => {
    // Full-page reload can restore the server `running` status before any
    // committed item or pending input has been rebuilt into a bubble. The
    // indicator must still stay visible for the main polly/custom-agent turn.
    expect(shouldShowWorkingIndicator(true, [])).toBe(true);
  });

  it("stays hidden for an empty idle session", () => {
    // Idle empty sessions should show the normal new-chat empty state, not the
    // busy indicator.
    expect(shouldShowWorkingIndicator(false, [])).toBe(false);
  });

  it("suppresses Working once a streaming assistant bubble is rendering content", () => {
    const bubbles: Bubble[] = [
      {
        kind: "assistant",
        responseId: "resp_live",
        stableId: "resp_live",
        lifecycle: "streaming",
        error: null,
        items: [{ kind: "text", itemId: null, text: "partial", final: false }],
      },
    ];

    expect(shouldShowWorkingIndicator(true, bubbles)).toBe(false);
  });

  it("lets an empty streaming assistant bubble keep the Working indicator visible", () => {
    const bubbles: Bubble[] = [
      {
        kind: "assistant",
        responseId: "resp_live",
        stableId: "resp_live",
        lifecycle: "streaming",
        error: null,
        items: [],
      },
    ];

    // Empty assistant shells do not yet prove content is rendering; hiding
    // Working here would recreate the blank gap this helper avoids.
    expect(shouldShowWorkingIndicator(true, bubbles)).toBe(true);
  });

  it("suppresses Working when a compaction loading bubble owns the active slot", () => {
    // Compaction loading already renders the busy state, so the standalone
    // Working indicator would be duplicate progress UI.
    expect(
      shouldShowWorkingIndicator(true, [{ kind: "compaction_loading", itemId: "cmp_1" }]),
    ).toBe(false);
  });
});

// ── workingIndicatorLabel ───────────────────────────────────────────────────

describe("workingIndicatorLabel", () => {
  it("shows the plain Working label when no background tasks remain", () => {
    expect(workingIndicatorLabel(0)).toBe("Working…");
  });

  it("treats a negative count as no background tasks", () => {
    // Defensive: the store seeds 0, but a stale/negative tally must never
    // produce a nonsensical "-1 background tasks" label.
    expect(workingIndicatorLabel(-1)).toBe("Working…");
  });

  it("uses the singular noun for exactly one background task", () => {
    expect(workingIndicatorLabel(1)).toBe("1 background task still running");
  });

  it("pluralizes the noun for more than one background task", () => {
    expect(workingIndicatorLabel(3)).toBe("3 background tasks still running");
  });
});

// ── subAgentComposerLabel ───────────────────────────────────────────────────

describe("subAgentComposerLabel", () => {
  /** Build the minimal session shape the helper reads. */
  const mkSession = (
    over: Partial<Parameters<typeof subAgentComposerLabel>[0] & object> = {},
  ): Parameters<typeof subAgentComposerLabel>[0] => ({
    parentSessionId: "conv_parent",
    title: null,
    subAgentName: null,
    agentName: null,
    ...over,
  });

  it("returns null for a top-level session (no parent)", () => {
    // No parent → not a sub-agent → no tray.
    expect(subAgentComposerLabel(mkSession({ parentSessionId: null }))).toBeNull();
  });

  it("returns null when no snapshot is loaded", () => {
    expect(subAgentComposerLabel(null)).toBeNull();
  });

  it("uses the title suffix after the first colon (spawn-seeded title)", () => {
    expect(
      subAgentComposerLabel(mkSession({ title: "claude_code:check-account-eligibility" })),
    ).toBe("check-account-eligibility");
  });

  it("keeps a colon inside the instance-name suffix", () => {
    // Only the first ":" separates tool from name; later colons belong to it.
    expect(subAgentComposerLabel(mkSession({ title: "researcher:auth:v2" }))).toBe("auth:v2");
  });

  it("strips the user-added 'ui:' sentinel before taking the suffix", () => {
    expect(subAgentComposerLabel(mkSession({ title: "ui:claude_code:my-task" }))).toBe("my-task");
  });

  it("returns the bare title when it has no colon", () => {
    expect(subAgentComposerLabel(mkSession({ title: "check-account-eligibility" }))).toBe(
      "check-account-eligibility",
    );
  });

  it("falls back to the sub-agent type when the title is unset", () => {
    expect(subAgentComposerLabel(mkSession({ title: null, subAgentName: "claude_code" }))).toBe(
      "claude_code",
    );
  });

  it("falls back to a generic label when every name field is null", () => {
    // Degenerate snapshot — the tray still needs something to render.
    expect(subAgentComposerLabel(mkSession())).toBe("sub-agent");
  });
});

// ── containsMarkdownTable ──────────────────────────────────────────────────

describe("containsMarkdownTable", () => {
  it("detects markdown tables in assistant text", () => {
    const items: RenderItem[] = [
      {
        kind: "text",
        itemId: "i1",
        text: "| Name | Value |\n| --- | --- |\n| Alpha | 1 |",
        final: true,
      },
    ];
    expect(containsMarkdownTable(items)).toBe(true);
  });

  it("does not treat ordinary pipe text as a table", () => {
    const items: RenderItem[] = [
      {
        kind: "text",
        itemId: "i1",
        text: "Use `cmd | grep foo` in a shell.",
        final: true,
      },
    ];
    expect(containsMarkdownTable(items)).toBe(false);
  });
});

// ── collectBubbleMarkdown ──────────────────────────────────────────────────

// Minimal ToolExecution fixture — only the fields collectBubbleMarkdown
// cares about (none; tool items are filtered out).
const TOOL_EXECUTION: ToolExecution = {
  name: "bash",
  callId: "c1",
  arguments: {},
  argsSummary: "bash",
  agentName: "agent",
  executedBy: "server",
  output: null,
};

describe("collectBubbleMarkdown", () => {
  it("returns empty string when there are no text items", () => {
    const items: RenderItem[] = [
      {
        kind: "tool",
        itemId: null,
        execution: TOOL_EXECUTION,
        output: "ok",
        state: "output-available",
        startedAt: null,
        duration: undefined,
      },
    ];
    // Empty string is falsy — the copy button should not appear
    expect(collectBubbleMarkdown(items)).toBe("");
  });

  it("returns the text of a single text item", () => {
    const items: RenderItem[] = [{ kind: "text", itemId: "i1", text: "Hello world", final: true }];
    expect(collectBubbleMarkdown(items)).toBe("Hello world");
  });

  it("joins multiple text items with two newlines between them", () => {
    const items: RenderItem[] = [
      { kind: "text", itemId: "i1", text: "First paragraph", final: true },
      { kind: "text", itemId: "i2", text: "Second paragraph", final: true },
    ];
    // Two newlines between blocks matches standard markdown paragraph separation.
    // Changing join("\n\n") to join("\n") would break this.
    expect(collectBubbleMarkdown(items)).toBe("First paragraph\n\nSecond paragraph");
  });

  it("excludes tool, reasoning, and error items from the copy text", () => {
    const items: RenderItem[] = [
      { kind: "text", itemId: "i1", text: "Before tool", final: true },
      {
        kind: "tool",
        itemId: "t1",
        execution: TOOL_EXECUTION,
        output: "result",
        state: "output-available",
        startedAt: null,
        duration: undefined,
      },
      {
        kind: "reasoning",
        itemId: "r1",
        text: "thinking…",
        duration: undefined,
      },
      { kind: "text", itemId: "i2", text: "After tool", final: true },
    ];
    // Only the two text items should appear; tool output and reasoning
    // are not part of the shareable prose.
    expect(collectBubbleMarkdown(items)).toBe("Before tool\n\nAfter tool");
  });

  it("trims leading and trailing whitespace from the assembled result", () => {
    const items: RenderItem[] = [
      { kind: "text", itemId: "i1", text: "  \n  trimmed  \n  ", final: true },
    ];
    // trim() ensures copy content doesn't start/end with blank lines
    expect(collectBubbleMarkdown(items)).toBe("trimmed");
  });
});

// ── Slash command map (skills + built-ins) ────────────────────────────

describe("buildSlashCommandMap", () => {
  it("returns the built-ins unchanged when no skills are loaded", () => {
    const map = buildSlashCommandMap([], true, true);
    // Insertion-order: built-ins come from the static record verbatim.
    expect(Object.keys(map)).toEqual(Object.keys(BUILTIN_SLASH_COMMANDS));
    // Spot-check a built-in description survives the spread.
    expect(map["/help"]).toBe(BUILTIN_SLASH_COMMANDS["/help"]);
  });

  it("omits /effort when effort controls are hidden", () => {
    const map = buildSlashCommandMap([], false, true);

    expect(map["/effort"]).toBeUndefined();
    expect(map["/compact"]).toBe(BUILTIN_SLASH_COMMANDS["/compact"]);
  });

  it("includes /model only when model override is supported", () => {
    // showModel=true → /model present (in-process harness, override
    // honored per turn). showModel=false → omitted (native-terminal
    // sessions: codex pins at launch, claude-native uses the picker).
    expect(buildSlashCommandMap([], true, true)["/model"]).toBe(BUILTIN_SLASH_COMMANDS["/model"]);
    expect(buildSlashCommandMap([], true, false)["/model"]).toBeUndefined();
    // Gating /model must not disturb the other built-ins.
    expect(buildSlashCommandMap([], true, false)["/compact"]).toBe(
      BUILTIN_SLASH_COMMANDS["/compact"],
    );
  });

  it("omits /compact when showCompact is false", () => {
    const map = buildSlashCommandMap([], true, true, false);
    expect(map["/compact"]).toBeUndefined();
    expect(map["/effort"]).toBe(BUILTIN_SLASH_COMMANDS["/effort"]);
    expect(map["/help"]).toBe(BUILTIN_SLASH_COMMANDS["/help"]);
  });

  it("includes /compact by default and when showCompact is true", () => {
    // Default (no showCompact arg) → included for backward compat.
    expect(buildSlashCommandMap([], true, true)["/compact"]).toBe(
      BUILTIN_SLASH_COMMANDS["/compact"],
    );
    // Explicit true → included.
    expect(buildSlashCommandMap([], true, true, true)["/compact"]).toBe(
      BUILTIN_SLASH_COMMANDS["/compact"],
    );
  });

  it("appends each skill as /<name> after the built-ins", () => {
    const map = buildSlashCommandMap(
      [
        { name: "triage-issues", description: "Triage issues." },
        { name: "mlflow-bug", description: "File an MLflow bug." },
      ],
      true,
      true,
    );
    // Built-ins first, then skills in their input order — the menu
    // surfaces built-ins above user skills.
    expect(Object.keys(map)).toEqual([
      ...Object.keys(BUILTIN_SLASH_COMMANDS),
      "/triage-issues",
      "/mlflow-bug",
    ]);
    expect(map["/triage-issues"]).toBe("Triage issues.");
    expect(map["/mlflow-bug"]).toBe("File an MLflow bug.");
  });

  it("matches skills via the same prefix filter the menu uses", () => {
    // The menu (SlashCommandMenu) filters keys whose ``name.slice(1)``
    // starts with the typed query. Verify the merged map plays nicely
    // with that filter for a partially-typed skill name.
    const map = buildSlashCommandMap(
      [
        { name: "triage-issues", description: "Triage issues." },
        { name: "mlflow-bug", description: "File an MLflow bug." },
      ],
      true,
      true,
    );
    const matches = Object.keys(map).filter((name) => name.slice(1).startsWith("tri"));
    expect(matches).toEqual(["/triage-issues"]);
  });
});

describe("buildSlashCommandWithArgsSet", () => {
  it("includes /effort even when no skills are loaded", () => {
    const s = buildSlashCommandWithArgsSet([], true, false);
    // ``/effort`` is the built-in that requires an arg; selecting it
    // from the menu fills "``/effort ``" rather than executing.
    expect(s.has("/effort")).toBe(true);
    expect(s.size).toBe(1);
  });

  it("omits /effort when effort controls are hidden", () => {
    const s = buildSlashCommandWithArgsSet([], false, false);

    expect(s.has("/effort")).toBe(false);
    // No arg-fill commands remain when effort is hidden and no skills are loaded.
    expect(s.size).toBe(0);
  });

  it("includes /model as an arg-fill command only when model override is supported", () => {
    // showModel toggles /model independently of /effort: each built-in
    // is gated on its own capability flag.
    expect(buildSlashCommandWithArgsSet([], false, true).has("/model")).toBe(true);
    expect(buildSlashCommandWithArgsSet([], false, true).size).toBe(1);
    expect(buildSlashCommandWithArgsSet([], false, false).has("/model")).toBe(false);
  });

  it("treats every skill as args-fill so the user can append context", () => {
    const s = buildSlashCommandWithArgsSet(
      [{ name: "triage-issues", description: "Triage issues." }],
      true,
      false,
    );
    // Skills are never auto-executed — they're sent as messages and
    // the harness dispatches them, so menu select should always fill.
    expect(s.has("/triage-issues")).toBe(true);
    expect(s.has("/effort")).toBe(true);
  });
});

describe("dispatchInitialPrompt", () => {
  // The wire-shape fork for the landing composer's first message. The
  // skill branch is what makes "/review-pr 123" typed on the landing page
  // actually invoke the skill — if it regresses to the plain path, the
  // agent receives literal "/review-pr 123" text and the skill never runs
  // (the original bug).
  it("posts a matched skill invocation as a slash_command, not a plain message", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
    dispatchInitialPrompt(
      {
        text: "/review-pr 123 focus on auth",
        skill: { name: "review-pr", args: "123 focus on auth" },
      },
      "ag_abc123",
      send,
      sendSlashCommand,
    );
    // Name (no leading slash) + raw args reach the slash_command path —
    // the exact values the server's skill lookup and the runner's
    // SKILL.md resolution key off.
    expect(sendSlashCommand).toHaveBeenCalledWith("review-pr", "123 focus on auth", "ag_abc123");
    // The plain path must NOT also fire — a double-send would deliver the
    // literal "/name" text alongside the skill invocation.
    expect(send).not.toHaveBeenCalled();
  });

  it("posts plain text (no matched skill) as a regular message", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
    dispatchInitialPrompt(
      { text: "read the README", skill: null },
      "ag_abc123",
      send,
      sendSlashCommand,
    );
    // Full text verbatim, no files. This is also the path for native
    // terminal sessions and unknown "/typo" commands (skill stays null).
    expect(send).toHaveBeenCalledWith("read the README", "ag_abc123", []);
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("carries landing attachments through the plain-message path", () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
    const file = new File(["x"], "diagram.png", { type: "image/png" });
    dispatchInitialPrompt(
      { text: "what is this?", skill: null, files: [file] },
      "ag_abc123",
      send,
      sendSlashCommand,
    );
    // The exact File objects picked on the landing screen reach send() —
    // an empty array here means first-message attachments silently vanish.
    expect(send).toHaveBeenCalledWith("what is this?", "ag_abc123", [file]);
  });
});

describe("shouldSendInitialPrompt", () => {
  // The "ready" baseline: a carried prompt, not yet sent, on a hydrated
  // session with a resolved agent. Each test below perturbs exactly one
  // field so the assertion pins which gate it controls — if a gate is
  // dropped in the effect, the matching case flips.
  const ready = {
    initialPrompt: "read the README",
    promptConversationId: "conv_abc",
    sentForConversationId: null,
    conversationId: "conv_abc",
    loadingConversation: false,
    agentId: "ag_abc123",
  } as const;

  it("sends when every gate passes", () => {
    // Proves the happy path fires. A failure here means a gate is
    // rejecting a fully-ready session — the prompt would never send.
    // Note the baseline carries NO runnerOnline field: runner liveness is
    // deliberately not a gate (the server holds the POST open while a
    // host-bound runner spins up), and the param was dropped from the
    // signature entirely — the type system, not a runtime test, is what
    // now prevents a runner gate from creeping back in.
    expect(shouldSendInitialPrompt(ready)).toBe(true);
  });

  it.each([
    ["null", null],
    ["empty string", ""],
  ] as const)("does not send when there is no carried prompt (%s)", (_label, initialPrompt) => {
    // null = user left the field blank (common case); "" = a
    // manipulated router state. Both are falsy and must never
    // auto-send — a failure would post an empty/garbage message.
    expect(shouldSendInitialPrompt({ ...ready, initialPrompt })).toBe(false);
  });

  it("does not send twice for the same conversation (once-guard)", () => {
    // sentForConversationId mirrors the effect's ref after the first
    // dispatch for this session. A failure means a re-render (e.g. runner
    // re-poll) would resend into the same conversation.
    expect(shouldSendInitialPrompt({ ...ready, sentForConversationId: "conv_abc" })).toBe(false);
  });

  it("sends again for a different conversation (per-session reset)", () => {
    // ChatPage stays mounted across `/c/:a` → `/c/:b` (no route key), so a
    // bare boolean once-guard would latch true after the first auto-send and
    // silently drop the prompt for every later new chat. Keying the guard by
    // conversation id resets it: a prompt consumed for conv_xyz still sends
    // even though conv_abc was already dispatched.
    expect(
      shouldSendInitialPrompt({
        ...ready,
        promptConversationId: "conv_xyz",
        sentForConversationId: "conv_abc",
        conversationId: "conv_xyz",
      }),
    ).toBe(true);
  });

  it("waits while the conversation is still hydrating", () => {
    // Sending mid-hydration races the stream bind. A failure means the
    // streamed response could be published before the subscriber exists.
    expect(shouldSendInitialPrompt({ ...ready, loadingConversation: true })).toBe(false);
  });

  it("waits until an agent is resolved", () => {
    // chatStore.send throws on a null agentId. A failure here means the
    // send would throw before agents finished loading.
    expect(shouldSendInitialPrompt({ ...ready, agentId: null })).toBe(false);
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
  ] as const)(
    "does not send on the new-chat landing (%s conversation id)",
    (_label, conversationId) => {
      // No session yet ("/" landing). The gate uses `!conversationId`, so
      // both null and undefined must reject — a change to `!== null` would
      // let undefined slip through and send into a non-existent session.
      expect(shouldSendInitialPrompt({ ...ready, conversationId })).toBe(false);
    },
  );

  it("does not send a prompt carried for one conversation into another (#flowing-message)", () => {
    // Repro for the session-switch leak: the landing composer stashes "P" for
    // conv_A and navigates to /c/A. Before the runner comes online the user
    // clicks conv_B (already running). The consume effect's setInitialPrompt
    // hasn't flushed yet, so the auto-send effect runs in the switch commit
    // with the STALE prompt (consumed for conv_A) but the NEW active session
    // (conv_B) — and send() pins the live store id, which is already conv_B.
    // Binding the prompt to its origin conversation lets the gate reject the
    // mismatch: a prompt consumed for conv_A must never post into conv_B.
    expect(
      shouldSendInitialPrompt({
        ...ready,
        promptConversationId: "conv_A",
        conversationId: "conv_B",
      }),
    ).toBe(false);
  });
});

describe("isSlashCommandText", () => {
  // The composer's command tint keys off this predicate, so it must match
  // real command drafts and reject prose / file paths.
  it.each([
    ["bare command", "/oncall"],
    ["command with args", "/cross-review fix the bug"],
    ["plugin-namespaced skill", "/dev-productivity:simplify"],
    ["leading/trailing whitespace", "  /compact  "],
  ] as const)("treats %s as a slash command", (_label, text) => {
    expect(isSlashCommandText(text)).toBe(true);
  });

  it.each([
    ["plain prose", "what does /foo do?"],
    ["absolute file path", "/etc/hosts is missing"],
    ["bare slash", "/"],
    ["empty string", ""],
  ] as const)("does not treat %s as a slash command", (_label, text) => {
    expect(isSlashCommandText(text)).toBe(false);
  });
});

describe("splitSlashCommand", () => {
  // The composer overlay tints only `token`; `before`/`after` stay default,
  // so the whole draft no longer goes blue once args are typed.
  it("splits the command token from its args", () => {
    expect(splitSlashCommand("/cross-review fix the bug")).toEqual({
      before: "",
      token: "/cross-review",
      after: " fix the bug",
    });
  });

  it("returns an empty `after` for a bare command", () => {
    expect(splitSlashCommand("/oncall")).toEqual({
      before: "",
      token: "/oncall",
      after: "",
    });
  });

  it("preserves leading whitespace in `before`", () => {
    expect(splitSlashCommand("  /compact")).toEqual({
      before: "  ",
      token: "/compact",
      after: "",
    });
  });

  it("keeps a plugin-namespaced skill name whole", () => {
    expect(splitSlashCommand("/dev-productivity:simplify now")).toEqual({
      before: "",
      token: "/dev-productivity:simplify",
      after: " now",
    });
  });

  it("returns null when the text isn't a command", () => {
    expect(splitSlashCommand("just prose")).toBeNull();
  });
});

describe("isUnboundCodingFork", () => {
  // The fork-source label is provenance and persists after the clone is
  // bound. Routing the directory picker on the label alone would reopen the
  // picker for a bound-but-offline fork — the bind endpoint then 400s
  // ("session already has a runner bound") and the queued prompt never
  // replays. The workspace gate (mirroring the server's needs_workspace)
  // closes that regression.
  it("is true for a fork with no workspace (never bound)", () => {
    expect(isUnboundCodingFork({ forkSourceId: "conv_src", workspace: null })).toBe(true);
  });

  it("treats undefined workspace as unbound", () => {
    expect(isUnboundCodingFork({ forkSourceId: "conv_src", workspace: undefined })).toBe(true);
  });

  it("is false once the fork is bound to a workspace", () => {
    // The regression guard: a bound fork (workspace set) that is merely
    // offline must NOT open the directory picker.
    expect(isUnboundCodingFork({ forkSourceId: "conv_src", workspace: "/Users/alice/repo" })).toBe(
      false,
    );
  });

  it("is false for a non-fork session regardless of workspace", () => {
    expect(isUnboundCodingFork({ forkSourceId: null, workspace: null })).toBe(false);
    expect(isUnboundCodingFork({ forkSourceId: null, workspace: "/Users/alice/repo" })).toBe(false);
  });

  it("treats an empty-string workspace as unbound (falsy)", () => {
    // An empty workspace string is not a real binding; the server's
    // needs_workspace is workspace IS NULL, and "" never satisfies the
    // workspace-required-for-host constraint — treat it as unbound.
    expect(isUnboundCodingFork({ forkSourceId: "conv_src", workspace: "" })).toBe(true);
  });
});
