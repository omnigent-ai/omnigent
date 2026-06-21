import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { BlockRenderer, PinnedElicitationIdContext } from "@/components/blocks/BlockRenderer";
import type { Bubble, RenderItem } from "@/lib/renderItems";
import { PinnedElicitationBar, selectPendingElicitation } from "./ChatPage";

// Issue #206: while a choice/elicitation is pending, the actionable card is
// pinned just above the composer (PinnedElicitationBar) and the inline copy
// in the transcript is suppressed (BlockRenderer + PinnedElicitationIdContext)
// so the prompt lives in exactly one place — where the user's attention lands.

type ElicitationItem = Extract<RenderItem, { kind: "elicitation" }>;

function pendingElicitation(elicitationId = "elic_1"): ElicitationItem {
  return {
    kind: "elicitation",
    itemId: `item_${elicitationId}`,
    elicitationId,
    message: "Approve the git push?",
    phase: "pre-tool",
    policyName: "blast_radius",
    contentPreview: "",
    requestedSchema: {},
    status: "pending",
    response: null,
  };
}

function respondedElicitation(elicitationId = "elic_1"): ElicitationItem {
  return {
    ...pendingElicitation(elicitationId),
    status: "responded",
    response: { action: "accept", content: undefined },
  };
}

afterEach(() => {
  cleanup();
});

describe("PinnedElicitationBar", () => {
  it("renders the pending card in the pinned slot above the composer", () => {
    // WHY: the actionable prompt must be visible at the bottom of the
    // conversation, not buried mid-transcript — the pinned slot is the fix.
    render(<PinnedElicitationBar elicitation={pendingElicitation()} />);
    expect(screen.getByTestId("pinned-elicitation")).toBeInTheDocument();
    const card = screen.getByTestId("approval-card");
    expect(card).toHaveAttribute("data-state", "pending");
    expect(within(card).getByText("Approval required")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByText(/Pinned · decision required/)).toBeInTheDocument();
  });

  it("renders nothing when no elicitation is pending", () => {
    // WHY: once the user resolves the prompt (or there never was one), the
    // pinned slot must vanish so the composer isn't pushed down by an empty
    // frame and the responded summary card returns to its inline place.
    const { container } = render(<PinnedElicitationBar elicitation={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("BlockRenderer inline suppression (issue #206)", () => {
  // BlockRenderer is exported; PinnedElicitationIdContext is the value ChatPage
  // threads in so the inline copy of the mirrored pending card is dropped.

  it("suppresses the inline pending card when its elicitationId is pinned", () => {
    // WHY: the pinned slot already owns this card; rendering it inline too
    // would duplicate the actionable prompt and split the user's focus.
    const elicitation = pendingElicitation("elic_pin");
    render(
      <PinnedElicitationIdContext.Provider value={elicitation.elicitationId}>
        <BlockRenderer items={[elicitation]} sessionStatus="idle" />
      </PinnedElicitationIdContext.Provider>,
    );
    expect(screen.queryByTestId("approval-card")).toBeNull();
  });

  it("renders the inline pending card when no elicitation is pinned", () => {
    // WHY: outside the chat (or before a pin exists), BlockRenderer must keep
    // rendering elicitations inline so the card is never lost entirely.
    render(
      <PinnedElicitationIdContext.Provider value={null}>
        <BlockRenderer items={[pendingElicitation()]} sessionStatus="idle" />
      </PinnedElicitationIdContext.Provider>,
    );
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-state", "pending");
  });

  it("renders a concurrent pending card inline even while a different one is pinned", () => {
    // WHY: only the ONE mirrored card is hoisted; a different pending prompt
    // (e.g. a sub-agent's) still renders inline so it isn't hidden.
    render(
      <PinnedElicitationIdContext.Provider value="elic_pinned">
        <BlockRenderer items={[pendingElicitation("elic_other")]} sessionStatus="idle" />
      </PinnedElicitationIdContext.Provider>,
    );
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-state", "pending");
  });

  it("always renders a resolved card inline even when its elicitationId is pinned", () => {
    // WHY: a resolved card is the historical record — it must return inline
    // once the user has decided, not stay suppressed by a stale pin value.
    render(
      <PinnedElicitationIdContext.Provider value="elic_1">
        <BlockRenderer items={[respondedElicitation("elic_1")]} sessionStatus="idle" />
      </PinnedElicitationIdContext.Provider>,
    );
    expect(screen.getByTestId("approval-card")).toHaveAttribute("data-state", "responded");
  });
});

type AssistantBubble = Extract<Bubble, { kind: "assistant" }>;

function assistantWith(items: RenderItem[], responseId: string): AssistantBubble {
  return {
    kind: "assistant",
    responseId,
    stableId: responseId,
    lifecycle: "completed",
    error: null,
    items,
  };
}

describe("selectPendingElicitation (issue #206 priority)", () => {
  // The selector is the pure back-to-front walk that picks which pending card
  // to pin. Only the LATEST pending elicitation is pinned; older pending cards
  // stay inline. Exercised here at the selector level (no React tree) so the
  // "two pending, latest wins" contract has a direct unit check.

  it("returns null when no elicitation is pending", () => {
    expect(selectPendingElicitation([assistantWith([respondedElicitation()], "r1")])).toBeNull();
    expect(selectPendingElicitation([])).toBeNull();
  });

  it("picks the single pending elicitation", () => {
    const elicitation = pendingElicitation();
    expect(selectPendingElicitation([assistantWith([elicitation], "r1")])).toBe(elicitation);
  });

  it("pins the latest pending elicitation when several are pending", () => {
    // WHY: a new prompt supersedes an older unresolved one — the user's next
    // selection resolves the most recent decision. The older card stays inline.
    const older = pendingElicitation("elic_old");
    const newer = pendingElicitation("elic_new");
    const selected = selectPendingElicitation([
      assistantWith([older], "r1"),
      assistantWith([newer], "r2"),
    ]);
    expect(selected?.elicitationId).toBe("elic_new");
  });

  it("ignores responded elicitations even if they appear later in the transcript", () => {
    // WHY: a resolved card is history, not an actionable prompt — a pending
    // card earlier in the transcript must still win over a responded one that
    // lands after it.
    const pending = pendingElicitation("elic_pending");
    const responded = respondedElicitation("elic_done");
    const selected = selectPendingElicitation([
      assistantWith([pending], "r1"),
      assistantWith([responded], "r2"),
    ]);
    expect(selected?.elicitationId).toBe("elic_pending");
  });

  it("skips non-assistant bubbles", () => {
    // WHY: user bubbles and other kinds never carry elicitations; the walk
    // must not blow up or pick up items from them.
    const elicitation = pendingElicitation();
    const selected = selectPendingElicitation([
      {
        kind: "user",
        itemId: "u1",
        content: [{ type: "input_text", text: "hi" }],
      },
      assistantWith([elicitation], "r1"),
    ]);
    expect(selected).toBe(elicitation);
  });
});
