import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  it("surfaces the recorded failure reason in the pill so a dead launch explains itself", () => {
    // WHY: a silently dead chat is the bug this pill exists to prevent — the
    // reason must reach the DOM, reachable in the pill's expandable body.
    render(<SandboxFailedIndicator status={{ stage: "failed", error: "out of quota" }} />);
    expect(screen.getByTestId("error-headline")).toHaveTextContent("Sandbox launch failed");
    fireEvent.click(screen.getByTestId("error-pill"));
    expect(screen.getByTestId("error-message-content")).toHaveTextContent("out of quota");
  });

  it("shows just the failure headline with no dangling colon when no reason is recorded", () => {
    // WHY: a missing reason must not render a dangling "failed: " — the pill
    // shows only the headline.
    render(<SandboxFailedIndicator status={{ stage: "failed", error: null }} />);
    expect(screen.getByTestId("error-headline")).toHaveTextContent("Sandbox launch failed");
    expect(screen.queryByText(/Sandbox launch failed:/)).not.toBeInTheDocument();
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

  it("renders nothing for a non-terminal-first host_offline session (badge owns the affordance)", () => {
    // WHY: the reconnect prompt for host_offline moved up into the composer's
    // host badge (ComposerStatusLine), where the host is already named — so
    // this band suppresses itself for a non-terminal-first session. The badge
    // renders the clickable "Host is offline — click to reconnect" instead.
    const { container } = render(
      <ConnectionIndicator
        liveness={{ kind: "host_offline", isOwner: true }}
        onShowReconnectHelp={onShowReconnectHelp}
      />,
    );
    expect(container).toBeEmptyDOMElement();
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
    // Explicitly settled: the fold-only cases below turn on `possiblyLive`
    // being false, so they must not ride on the store's default status.
    useChatStore.setState({ conversationId: "conv_test", sessionStatus: "idle" });
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
    expect(bubble).toHaveClass("max-w-[640px]");
    expect(bubble).toHaveTextContent("hello there");
  });

  it("renders an assistant text bubble with a copy action", () => {
    // WHY: assistant branch — prose renders and the copy affordance appears
    // whenever there's collectable markdown.
    render(<BubbleView bubble={assistantText("the answer is 42")} />);
    const bubble = screen.getByTestId("message-bubble");
    expect(bubble).toHaveAttribute("data-role", "assistant");
    expect(bubble).toHaveClass("gap-2");
    expect(bubble).toHaveTextContent("the answer is 42");
    expect(screen.getByRole("button", { name: "Copy" })).toHaveAttribute("data-size", "icon-xxs");
  });

  it("uses full-width layout for assistant display math", () => {
    render(<BubbleView bubble={assistantText(String.raw`$$d = \sqrt{x^2 + y^2}$$`)} />);

    const bubble = screen.getByTestId("message-bubble");
    expect(bubble).toHaveClass("max-w-full");
    expect(bubble.firstElementChild).toHaveClass("w-full");
  });

  const errorItem = (): AssistantBubble["items"][number] => ({
    kind: "error",
    itemId: "err_1",
    message: "Required terminal exited unexpectedly; the runtime is no longer available.",
    source: "execution",
    code: "required_terminal_exited",
  });

  const errorBubble = (items: AssistantBubble["items"]): AssistantBubble => ({
    kind: "assistant",
    responseId: "resp_err",
    stableId: "resp_err",
    lifecycle: "completed",
    error: null,
    items,
    createdAtS: 1_750_000_000,
  });

  it("spans the chat column for an error-only bubble, with no hover footer", () => {
    // WHY: an error banner is a thread-level element — its dashed rule must
    // span the width a long-text turn takes (not shrink-wrap the 560px pill
    // via MessageContent's w-fit), and the timestamp/actions chrome belongs
    // to assistant prose, never to the error.
    render(<BubbleView bubble={errorBubble([errorItem()])} />);

    const bubble = screen.getByTestId("message-bubble");
    expect(screen.getByTestId("error-pill")).toBeInTheDocument();
    expect(bubble).toHaveClass("max-w-full");
    expect(bubble.firstElementChild).toHaveClass("w-full");
    expect(screen.queryByTestId("message-timestamp")).not.toBeInTheDocument();
  });

  it("spans the chat column for a text+error turn and keeps the footer", () => {
    // WHY: a mid-turn error shares the bubble with prose — the rule still
    // spans the full column even when the prose is short, and the footer
    // stays because the timestamp/actions describe the text.
    render(
      <BubbleView
        bubble={errorBubble([
          { kind: "text", itemId: "t1", text: "short answer", final: true },
          errorItem(),
        ])}
      />,
    );

    const bubble = screen.getByTestId("message-bubble");
    expect(screen.getByTestId("error-pill")).toBeInTheDocument();
    expect(bubble).toHaveClass("max-w-full");
    expect(bubble.firstElementChild).toHaveClass("w-full");
    expect(screen.getByTestId("message-timestamp")).toBeInTheDocument();
  });

  it("marks a cancelled assistant turn as Interrupted", () => {
    // WHY: the cancelled lifecycle branch surfaces an explicit Interrupted
    // note so a truncated turn doesn't read as a complete answer.
    render(<BubbleView bubble={assistantText("partial", "cancelled")} />);
    expect(screen.getByTestId("assistant-interrupted-indicator")).toHaveTextContent("Interrupted");
  });

  it("surfaces a failed turn's error as a destructive pill, never raw text", () => {
    // WHY: a failed send/stream must render through the same error pill an error
    // block uses — never bare red "Error:" text — and the message stays
    // reachable in the pill's expandable body so a dead turn still explains itself.
    render(<BubbleView bubble={{ ...assistantText("", "failed"), error: "rate limited" }} />);
    const pill = screen.getByTestId("error-pill");
    expect(pill).toBeInTheDocument();
    expect(screen.queryByText(/^Error:/)).not.toBeInTheDocument();
    fireEvent.click(pill);
    expect(screen.getByTestId("error-message-content")).toHaveTextContent("rate limited");
  });

  it("adds no error pill for a failed turn whose message rides on its block", () => {
    // WHY: a dropped-host turn ends "failed" but carries its explanation as an
    // error block inside the bubble, leaving `error` null — the bubble must add
    // no second pill (and never a bare "Error:") of its own.
    render(<BubbleView bubble={{ ...assistantText("", "failed"), error: null }} />);
    expect(screen.queryByTestId("error-pill")).not.toBeInTheDocument();
    expect(screen.queryByText(/Error:/)).not.toBeInTheDocument();
  });

  const toolItem = (callId: string): AssistantBubble["items"][number] => ({
    kind: "tool",
    itemId: callId,
    execution: {
      name: "Bash",
      arguments: { command: "ls" },
      argsSummary: "ls",
      callId,
      agentName: "coder",
      executedBy: "server",
      output: "ok",
    },
    output: "ok",
    state: "output-available",
    startedAt: 0,
    duration: 1,
  });

  /** A turn that yielded mid-task: its whole trace folds, its answer lands later. */
  const foldOnlyBubble = (items: AssistantBubble["items"]): AssistantBubble => ({
    kind: "assistant",
    responseId: "resp_fold",
    stableId: "resp_fold",
    lifecycle: "completed",
    error: null,
    items,
    workedForS: 147,
    continued: true,
  });

  it("gives fold-only turns the same chrome whether or not the hidden trace narrated", () => {
    // WHY: the copy/fork row keys off ALL the bubble's text, including text
    // sealed inside the fold — so an otherwise identical collapsed row grew a
    // row of hover-only chrome purely because its hidden trace happened to
    // narrate, and consecutive "Worked for" rows sat at two different gaps.
    render(
      <BubbleView
        bubble={foldOnlyBubble([
          { kind: "text", itemId: "t1", text: "Let me look at the code.", final: false },
          toolItem("c1"),
        ])}
      />,
    );
    expect(screen.getByTestId("turn-worked-fold")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    // Nothing below the fold: the collapsed row is the bubble's whole height.
    expect(screen.getByTestId("message-bubble").children).toHaveLength(1);
    cleanup();

    render(<BubbleView bubble={foldOnlyBubble([toolItem("c2"), toolItem("c3")])} />);
    expect(screen.getByTestId("turn-worked-fold")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    expect(screen.getByTestId("message-bubble").children).toHaveLength(1);
  });

  it("spans the column for a fold-only turn so the row's hairline draws", () => {
    // WHY: shrink-wrapped to the ~110px summary row, the trailing hairline
    // (a flex-1 span) collapses to zero width and the click target stops
    // short of the column. The max-w-3xl cap keeps it aligned with the rule
    // under an answered turn.
    render(<BubbleView bubble={foldOnlyBubble([toolItem("c4")])} />);
    const bubble = screen.getByTestId("message-bubble");
    expect(bubble).toHaveClass("max-w-3xl", "min-[2561px]:max-w-[clamp(56rem,30vw,64rem)]");
    expect(bubble.firstElementChild).toHaveClass("w-full");
    expect(bubble.firstElementChild).not.toHaveClass("w-fit");
  });

  it("keeps the copy action on a folded turn that answers for itself", () => {
    // WHY: the suppression must stop at bubbles with a visible answer —
    // that trailing prose is exactly what copy/fork act on.
    render(
      <BubbleView
        bubble={{
          ...foldOnlyBubble([
            toolItem("c5"),
            { kind: "text", itemId: "t2", text: "Done — here's the fix.", final: true },
          ]),
          continued: false,
        }}
      />,
    );
    expect(screen.getByTestId("turn-worked-fold")).toBeInTheDocument();
    expect(screen.getByText("Done — here's the fix.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy" })).toBeInTheDocument();
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
