// A deployment can name which agents New Chat offers (server config
// `restrict_to_agent_names`). The landing screen filters the roster and hands
// the picker the result as `curatedEntries`; these cover what that does to the
// menu's shape. Default selection and the real-flow empty state live with the
// landing screen's tests.
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentHarnessPicker } from "./NewChatDialog";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { TooltipProvider } from "@/components/ui/tooltip";

const agent = (
  overrides: Partial<AvailableAgent> &
    Pick<AvailableAgent, "id" | "name" | "display_name" | "harness">,
): AvailableAgent => ({ description: null, skills: [], builtin: true, ...overrides });

const claude = agent({
  id: "agent-claude",
  name: "claude-native-ui",
  display_name: "Claude Code",
  harness: "claude-native",
});
const codex = agent({
  id: "agent-codex",
  name: "codex-native-ui",
  display_name: "Codex",
  harness: "codex-native",
});
const polly = agent({
  id: "agent-polly",
  name: "polly",
  display_name: "Polly",
  harness: "claude-sdk",
});

const readyHost: Host = {
  host_id: "host-1",
  name: "laptop",
  owner: "me",
  status: "online",
  configured_harnesses: {
    "claude-native": true,
    "codex-native": true,
    "claude-sdk": true,
  },
};

function renderPicker(props: {
  harnessEntries: AvailableAgent[];
  agentEntries?: AvailableAgent[];
  curatedEntries?: AvailableAgent[];
  hasAgents?: boolean;
}) {
  return render(
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter>
        <TooltipProvider>
          <AgentHarnessPicker
            agentEntries={props.agentEntries ?? []}
            harnessEntries={props.harnessEntries}
            curatedEntries={props.curatedEntries}
            effectiveAgentId={null}
            agentLabel="Picker"
            hasAgents={props.hasAgents ?? true}
            host={readyHost}
            onSelectAgent={() => undefined}
            pendingAgent={null}
            pendingAgentId="pending-agent"
            onSelectPending={() => undefined}
            onCreateCustomAgent={() => undefined}
            sandboxSelected={false}
            allowCreateCustomAgent
          />
        </TooltipProvider>
      </StoryQueryRouter>
    </CapabilitiesProvider>,
  );
}

const openPicker = () => userEvent.click(screen.getByTestId("new-chat-landing-agent-select"));
const curatedLabels = () =>
  [
    ...screen
      .getByTestId("new-chat-landing-curated")
      .querySelectorAll("[data-harness-menu-choice-label]"),
  ].map((node) => node.textContent);

afterEach(cleanup);

describe("a curated New Chat picker", () => {
  it.each([
    // The deployment's own pick leads, which is the point of configuring it.
    { roster: "two agents", curatedEntries: [polly, codex], labels: ["Polly", "Codex"] },
    // One agent is the common case: the picker collapses to that row.
    { roster: "one agent", curatedEntries: [polly], labels: ["Polly"] },
  ])(
    "offers exactly the handed-down $roster, in that order",
    async ({ curatedEntries, labels }) => {
      renderPicker({ harnessEntries: [claude, codex], agentEntries: [polly], curatedEntries });
      await openPicker();

      expect(curatedLabels()).toEqual(labels);
      expect(screen.queryByText("Claude Code")).toBeNull();
    },
  );

  it("drops the groups and actions that can no longer lead anywhere", async () => {
    renderPicker({
      harnessEntries: [claude, codex],
      agentEntries: [polly],
      curatedEntries: [polly, codex],
    });
    await openPicker();

    // No Harnesses heading: the roster is one list, not a split one.
    expect(screen.queryByText("Harnesses")).toBeNull();
    // Nothing is folded away, so there is no "Other..." to open.
    expect(screen.queryByTestId("new-chat-landing-harness-more")).toBeNull();
    expect(screen.queryByTestId("new-chat-landing-custom-agents")).toBeNull();
    // Creating an agent would produce one the picker then refuses to show.
    expect(screen.queryByTestId("new-chat-landing-create-agent")).toBeNull();
  });

  it("still opens when the curation matches nothing, so the empty state can explain", async () => {
    // The caller reports no agents, which would normally disable the trigger.
    renderPicker({ harnessEntries: [claude], curatedEntries: [], hasAgents: false });

    expect(screen.getByTestId("new-chat-landing-agent-select")).toBeEnabled();
    await openPicker();
    expect(screen.getByTestId("new-chat-landing-no-agents")).toHaveTextContent(
      "limits which agents New Chat offers",
    );
  });

  it("leaves the grouped menu alone when no curation is handed down", async () => {
    // Also what every other embedder gets: curation is the landing's to pass.
    renderPicker({ harnessEntries: [claude, codex], agentEntries: [polly] });
    await openPicker();

    expect(screen.queryByTestId("new-chat-landing-curated")).toBeNull();
    expect(screen.getByText("Harnesses")).toBeVisible();
    // Creating stays reachable, in the submenu that groups custom agents.
    expect(screen.getByTestId("new-chat-landing-custom-agents")).toBeVisible();
  });
});
