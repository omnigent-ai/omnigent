import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SwitchAgentDialog } from "./SwitchAgentDialog";
import { switchSessionAgent } from "@/lib/sessionsApi";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useSessionAgent } from "@/hooks/useAgents";

vi.mock("@/lib/sessionsApi", () => ({ switchSessionAgent: vi.fn() }));
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/hooks/useAgents", () => ({ useSessionAgent: vi.fn() }));

const switchSessionAgentMock = vi.mocked(switchSessionAgent);
const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const useSessionAgentMock = vi.mocked(useSessionAgent);

// One agent per shape so the history-preservation filter is exercised:
// an anthropic SDK + native, an openai SDK + native.
const AVAILABLE_AGENTS = [
  {
    id: "ag_claude_sdk",
    name: "claude",
    display_name: "Claude",
    description: null,
    harness: "claude-sdk",
  },
  {
    id: "ag_claude_native",
    name: "claude-native-ui",
    display_name: "Claude Code",
    description: null,
    harness: "claude-native",
  },
  {
    id: "ag_codex_native",
    name: "codex-native-ui",
    display_name: "Codex",
    description: null,
    harness: "codex-native",
  },
  {
    id: "ag_openai",
    name: "gpt",
    display_name: "GPT",
    description: null,
    harness: "openai-agents",
  },
];

function setAgents(current: { id: string; name: string; harness: string | null }): void {
  useAvailableAgentsMock.mockReturnValue({
    data: AVAILABLE_AGENTS,
  } as unknown as ReturnType<typeof useAvailableAgents>);
  useSessionAgentMock.mockReturnValue({
    data: current,
  } as unknown as ReturnType<typeof useSessionAgent>);
}

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const onOpenChange = vi.fn();
  const utils = render(
    <QueryClientProvider client={client}>
      <SwitchAgentDialog sessionId="conv_src" open onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  );
  return { ...utils, invalidateSpy, onOpenChange };
}

/** Open the Radix agent <Select> (mirrors ForkSessionDialog.test). */
function openAgentSelect(): void {
  const trigger = screen.getByTestId("switch-agent-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

beforeEach(() => {
  switchSessionAgentMock.mockReset();
  // Default: the session currently runs claude-sdk (anthropic).
  setAgents({ id: "ag_source", name: "source", harness: "claude-sdk" });
});

afterEach(cleanup);

describe("SwitchAgentDialog", () => {
  it("offers history-preserving targets including cross-family codex-native", () => {
    // Current harness is claude-sdk (anthropic): every classifiable target
    // carries history — SDK targets as replayed context, native targets via
    // rebuild-from-items (the codex rollout synthesizer writes the
    // session_meta + event_msg records codex ≥ 0.133 needs, verified on
    // 0.136.0), so cross-family codex-native is offered too.
    renderDialog();
    openAgentSelect();

    expect(screen.getByTestId("switch-agent-option-ag_claude_sdk")).toBeInTheDocument();
    // Same-family native carries history via rebuild-from-items.
    expect(screen.getByTestId("switch-agent-option-ag_claude_native")).toBeInTheDocument();
    // Cross-family SDK still carries history as replayed context.
    expect(screen.getByTestId("switch-agent-option-ag_openai")).toBeInTheDocument();
    // Cross-family codex-native carries history via rebuild-from-items.
    expect(screen.getByTestId("switch-agent-option-ag_codex_native")).toBeInTheDocument();
  });

  it("excludes the session's own agent even when it's a '(switch …)' clone", () => {
    // The bound agent is a session-scoped clone named "<builtin> (switch
    // ag_…)". The dedup strips the suffix so the built-in it derives from is
    // not offered (switching to it would be a no-op).
    setAgents({
      id: "ag_src_clone",
      name: "claude (switch ag_9f8e7d6)",
      harness: "claude-sdk",
    });
    renderDialog();
    openAgentSelect();

    // The built-in the clone derives from (claude → ag_claude_sdk) is hidden.
    expect(screen.queryByTestId("switch-agent-option-ag_claude_sdk")).not.toBeInTheDocument();
    // A different same-family target (claude-native) is still offered.
    expect(screen.getByTestId("switch-agent-option-ag_claude_native")).toBeInTheDocument();
  });

  it("excludes the origin built-in and labels it for a fork-of-a-fork clone", () => {
    // A fork of a fork nests suffixes: "claude (fork …) (fork …)". A
    // single-layer strip would leave "claude (fork …)" — not a built-in
    // name — so the origin would wrongly be offered AND the current label
    // would show the raw suffixed slug. agentRootName peels to "claude".
    setAgents({
      id: "ag_src_clone2",
      name: "claude (fork ag_a) (fork ag_b)",
      harness: "claude-sdk",
    });
    renderDialog();

    // Current-agent label resolves to the origin built-in's display name,
    // not the raw "claude (fork ag_a)" a one-layer strip would leave.
    expect(screen.getByTestId("switch-agent-current")).toHaveTextContent("Claude");

    openAgentSelect();
    // The origin built-in (claude → ag_claude_sdk) is still excluded.
    expect(screen.queryByTestId("switch-agent-option-ag_claude_sdk")).not.toBeInTheDocument();
    expect(screen.getByTestId("switch-agent-option-ag_claude_native")).toBeInTheDocument();
  });

  it("submit is disabled until a target is chosen", () => {
    renderDialog();
    // No selection yet → the Switch button is disabled, so a stray click
    // can't fire a switch with no agent_id.
    expect(screen.getByTestId("switch-agent-submit")).toBeDisabled();
  });

  it("shows the current agent as a greyed default (not a blank box)", () => {
    // The bound agent's name matches a built-in, so its display_name shows.
    // Regression: a non-empty sentinel value rendered a BLANK trigger because
    // Radix suppresses the placeholder unless the value is empty/undefined.
    setAgents({ id: "ag_cur", name: "claude", harness: "claude-sdk" });
    renderDialog();
    const current = screen.getByTestId("switch-agent-current");
    expect(current).toHaveTextContent("Claude");
    expect(current).toHaveTextContent("(current agent)");
    // It's a default hint, not a selection — submit stays disabled until the
    // user picks a different agent.
    expect(screen.getByTestId("switch-agent-submit")).toBeDisabled();
  });

  it("switches to the chosen agent, refreshes the bound agent + list, and closes", async () => {
    switchSessionAgentMock.mockResolvedValue({
      id: "conv_src",
    } as unknown as Awaited<ReturnType<typeof switchSessionAgent>>);

    const { invalidateSpy, onOpenChange } = renderDialog();

    openAgentSelect();
    fireEvent.click(screen.getByTestId("switch-agent-option-ag_claude_native"));
    fireEvent.click(screen.getByTestId("switch-agent-submit"));

    await waitFor(() => expect(switchSessionAgentMock).toHaveBeenCalledTimes(1));
    // The SAME session id and the chosen target are sent — proves the in-place
    // switch targets this session (not a fork) with the picked agent.
    expect(switchSessionAgentMock).toHaveBeenCalledWith("conv_src", "ag_claude_native");
    // Bound-agent query refreshed so the header/pill shows the new harness,
    // the sidebar list refreshed, and the per-session snapshot invalidated so
    // the model settings + labels the switch reset don't linger stale (it uses
    // staleTime: Infinity and won't refetch otherwise).
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session", "conv_src"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["session-agent", "conv_src"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["conversations"] });
    // Dialog closes on success.
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("warns that model settings reset on a cross-family switch", () => {
    // Current is claude-sdk (anthropic); choosing the openai SDK target
    // crosses provider families, so the server resets model + effort. The
    // dialog must warn before the user commits.
    renderDialog();
    openAgentSelect();
    fireEvent.click(screen.getByTestId("switch-agent-option-ag_openai"));

    expect(screen.getByTestId("switch-agent-reset-warning")).toBeInTheDocument();
  });

  it("does not warn about reset on a same-family switch", () => {
    // claude-sdk → claude-native stays within anthropic, so model settings
    // carry over and no reset warning shows.
    renderDialog();
    openAgentSelect();
    fireEvent.click(screen.getByTestId("switch-agent-option-ag_claude_native"));

    expect(screen.queryByTestId("switch-agent-reset-warning")).not.toBeInTheDocument();
  });

  it("surfaces the server error inline on failure and stays open", async () => {
    switchSessionAgentMock.mockRejectedValue(new Error("409 Session is busy"));
    const { onOpenChange } = renderDialog();

    openAgentSelect();
    fireEvent.click(screen.getByTestId("switch-agent-option-ag_claude_native"));
    fireEvent.click(screen.getByTestId("switch-agent-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("switch-agent-error")).toHaveTextContent("409 Session is busy"),
    );
    // A failed switch must not close the dialog (so the user can retry).
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
  });
});
