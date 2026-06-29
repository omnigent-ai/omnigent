import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useChatStore } from "@/store/chatStore";
import type { Bubble } from "@/lib/renderItems";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import {
  BubbleView,
  ConnectionIndicator,
  RunnerStartingIndicator,
  SandboxFailedIndicator,
} from "./ChatPage";

// Render-level coverage for the chat surface's status bands and bubble
// dispatcher. These exercise the branches that the pure-helper tests can't:
// what the user actually SEES for a failed sandbox, an offline host, an
// in-flight launch, and each bubble kind. They run the real component tree
// (no mocks) the same way ChatPage.composer.test.tsx renders the Composer.

afterEach(() => {
  // Several tests poke sandboxStatus into the global zustand store; reset it
  // so a leftover launch band can't bleed into the next test.
  useChatStore.setState({ sandboxStatus: null });
  cleanup();
});

describe("SandboxFailedIndicator", () => {
  it("renders the recorded failure reason so a dead launch explains itself", () => {
    // WHY: a silently dead chat is the bug this band exists to prevent — the
    // reason must reach the DOM.
    render(<SandboxFailedIndicator status={{ stage: "failed", error: "out of quota" }} />);
    expect(screen.getByText(/Sandbox launch failed: out of quota/)).toBeInTheDocument();
  });

  it("omits the colon suffix when no error detail is recorded", () => {
    // WHY: a missing error must not render a dangling "failed: " — the
    // ternary guards the suffix.
    render(<SandboxFailedIndicator status={{ stage: "failed", error: null }} />);
    expect(screen.getByText("Sandbox launch failed")).toBeInTheDocument();
  });
});

describe("ConnectionIndicator", () => {
  const onShowReconnectHelp = () => {};

  it("renders the failed-sandbox band when a launch died (sandboxStatus wins)", () => {
    // WHY: a failed launch owns this band ahead of any liveness state — the
    // sandbox branch short-circuits before the liveness checks.
    useChatStore.setState({ sandboxStatus: { stage: "failed", error: "boom" } });
    render(
      <ConnectionIndicator
        liveness={{ kind: "online" }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    expect(screen.getByTestId("sandbox-failed-indicator")).toBeInTheDocument();
  });

  it("renders nothing while a launch is still in flight (non-failed sandbox)", () => {
    // WHY: an in-flight launch renders in the thread (RunnerStartingIndicator),
    // so this band suppresses itself to avoid double progress UI.
    useChatStore.setState({ sandboxStatus: { stage: "provisioning" } });
    const { container } = render(
      <ConnectionIndicator
        liveness={{ kind: "host_offline", isOwner: true }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the host-offline reconnect affordance", () => {
    // WHY: a host_offline session is unreachable — the only way back is the
    // clickable reconnect banner with the host-specific copy.
    render(
      <ConnectionIndicator
        liveness={{ kind: "host_offline", isOwner: true }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    const btn = screen.getByTestId("disconnected-indicator");
    expect(btn).toHaveTextContent(/Host is offline/);
  });

  it("shows agent-disconnected copy for a local-stranded runner", () => {
    // WHY: local_stranded is the other unreachable branch and must read as the
    // agent dropping, not the host.
    render(
      <ConnectionIndicator
        liveness={{ kind: "local_stranded" }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    expect(screen.getByTestId("disconnected-indicator")).toHaveTextContent(/Agent disconnected/);
  });

  it("shows a passive Connecting row for a starting non-terminal session", () => {
    // WHY: a runner spinning up gets a heartbeat (no action) so the empty chat
    // doesn't read as broken.
    render(
      <ConnectionIndicator
        liveness={{ kind: "starting" }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    expect(screen.getByTestId("connecting-indicator")).toHaveTextContent("Connecting…");
  });

  it.each<SessionLiveness>([{ kind: "online" }, { kind: "runner_asleep" }, { kind: "unknown" }])(
    "renders nothing for the reachable/sidebar-owned state %o",
    (liveness) => {
      // WHY: online/asleep/unknown surface their status in the sidebar or keep
      // the composer open — this band stays empty for them.
      const { container } = render(
        <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );
});

describe("RunnerStartingIndicator", () => {
  it("shows the stage-specific copy for an in-flight sandbox launch", () => {
    // WHY: the band names the current pipeline stage so the wait is legible;
    // "cloning" must map to the repo-clone copy.
    useChatStore.setState({ sandboxStatus: { stage: "cloning" } });
    render(<RunnerStartingIndicator variant="row" />);
    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Cloning repository…",
    );
  });

  it("renders the hero variant with the stage title for an empty-state launch", () => {
    // WHY: the hero variant is the centered empty-state placeholder; it must
    // carry the same stage label as a heading, not the row copy.
    useChatStore.setState({ sandboxStatus: { stage: "provisioning" } });
    render(<RunnerStartingIndicator variant="hero" />);
    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Provisioning sandbox…",
    );
  });

  it("self-gates to null when no launch is in flight (no terminal-first ctx)", () => {
    // WHY: with no sandbox launch and no terminal-first provider, neither
    // launch shape applies and the indicator must render nothing.
    const { container } = render(<RunnerStartingIndicator variant="row" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a terminal sandbox stage (ready/failed handled elsewhere)", () => {
    // WHY: "failed" gets the destructive band in ConnectionIndicator, so this
    // in-thread indicator must skip it rather than show stale launch copy.
    useChatStore.setState({ sandboxStatus: { stage: "failed", error: "x" } });
    const { container } = render(<RunnerStartingIndicator variant="row" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("BubbleView dispatch", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test" });
  });

  type AssistantBubble = Extract<Bubble, { kind: "assistant" }>;
  const assistantText = (
    text: string,
    lifecycle: AssistantBubble["lifecycle"] = "completed",
  ): AssistantBubble => ({
    kind: "assistant",
    responseId: "resp_1",
    stableId: "resp_1",
    lifecycle,
    error: null,
    items: [{ kind: "text", itemId: "i1", text, final: true }],
  });

  it("renders a plain user message as a user bubble", () => {
    // WHY: the user branch of the dispatcher — text content renders inside a
    // user-role bubble.
    render(
      <BubbleView
        bubble={{
          kind: "user",
          itemId: "u1",
          content: [{ type: "input_text", text: "hello there" }],
        }}
      />,
    );
    const bubble = screen.getByTestId("message-bubble");
    expect(bubble).toHaveAttribute("data-role", "user");
    expect(bubble).toHaveTextContent("hello there");
  });

  it("renders an assistant text bubble with a copy action", () => {
    // WHY: assistant branch — prose renders and the copy affordance appears
    // whenever there's collectable markdown.
    render(<BubbleView bubble={assistantText("the answer is 42")} />);
    const bubble = screen.getByTestId("message-bubble");
    expect(bubble).toHaveAttribute("data-role", "assistant");
    expect(bubble).toHaveTextContent("the answer is 42");
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
  });

  it("marks a cancelled assistant turn as Interrupted", () => {
    // WHY: the cancelled lifecycle branch surfaces an explicit Interrupted
    // note so a truncated turn doesn't read as a complete answer.
    render(<BubbleView bubble={assistantText("partial", "cancelled")} />);
    expect(screen.getByTestId("assistant-interrupted-indicator")).toHaveTextContent("Interrupted");
  });

  it("renders the error text for a failed assistant turn", () => {
    // WHY: the failed branch must surface the error so a dead turn explains
    // itself instead of vanishing.
    render(<BubbleView bubble={{ ...assistantText("", "failed"), error: "rate limited" }} />);
    expect(screen.getByText(/Error: rate limited/)).toBeInTheDocument();
  });

  it("renders the compacting shimmer for a compaction_loading bubble", () => {
    // WHY: the compaction_loading branch owns the busy slot during context
    // compaction — it must show its own indicator.
    render(<BubbleView bubble={{ kind: "compaction_loading", itemId: "cmp_1" }} />);
    expect(screen.getByTestId("compacting-indicator")).toHaveTextContent(
      "Compacting conversation…",
    );
  });
});
