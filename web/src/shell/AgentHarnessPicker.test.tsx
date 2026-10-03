import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentHarnessPicker } from "./NewChatDialog";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";

const agent = (
  overrides: Partial<AvailableAgent> &
    Pick<AvailableAgent, "id" | "name" | "display_name" | "harness">,
): AvailableAgent => ({
  description: null,
  skills: [],
  builtin: true,
  ...overrides,
});

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
const pi = agent({
  id: "agent-pi",
  name: "pi-native-ui",
  display_name: "Pi",
  harness: "pi-native",
});
const openCode = agent({
  id: "agent-opencode",
  name: "opencode-native-ui",
  display_name: "OpenCode",
  harness: "opencode-native",
});
const kiro = agent({
  id: "agent-kiro",
  name: "kiro-native-ui",
  display_name: "Kiro",
  harness: "kiro-native",
});
const polly = agent({
  id: "agent-polly",
  name: "polly",
  display_name: "Polly",
  harness: "claude-sdk",
});

const readyHost: Host = {
  host_id: "host-test",
  name: "Dev Box",
  owner: "developer",
  status: "online",
  configured_harnesses: {
    "claude-sdk": true,
    "claude-native": true,
    "codex-native": true,
    "pi-native": true,
    "opencode-native": true,
    "kiro-native": true,
  },
};

function renderPicker(props: {
  harnessEntries: AvailableAgent[];
  agentEntries?: AvailableAgent[];
  effectiveAgentId?: string | null;
  allowCreateCustomAgent?: boolean;
  sandboxSelected?: boolean;
}) {
  return render(
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter>
        <AgentHarnessPicker
          agentEntries={props.agentEntries ?? []}
          harnessEntries={props.harnessEntries}
          effectiveAgentId={props.effectiveAgentId ?? null}
          agentLabel="Picker"
          hasAgents
          host={readyHost}
          onSelectAgent={() => undefined}
          pendingAgent={null}
          pendingAgentId="pending-agent"
          onSelectPending={() => undefined}
          onCreateCustomAgent={() => undefined}
          sandboxSelected={props.sandboxSelected ?? false}
          allowCreateCustomAgent={props.allowCreateCustomAgent ?? true}
        />
      </StoryQueryRouter>
    </CapabilitiesProvider>,
  );
}

async function openPicker(): Promise<void> {
  await userEvent.click(screen.getByTestId("new-chat-landing-agent-select"));
}

/** Harness rows rendered inline, i.e. not inside the "Other..." submenu. */
function inlineHarnessIds(ids: string[]): string[] {
  return ids.filter((id) => screen.queryByTestId(`new-chat-landing-agent-${id}`) !== null);
}

afterEach(cleanup);

describe("AgentHarnessPicker inline harness list", () => {
  it("keeps the preferred harnesses inline when they are seeded", async () => {
    renderPicker({ harnessEntries: [claude, codex, pi], effectiveAgentId: claude.id });
    await openPicker();

    // claude + codex are preferred and stay inline; pi stays behind "Other...".
    expect(inlineHarnessIds([claude.id, codex.id])).toEqual([claude.id, codex.id]);
    expect(screen.queryByTestId(`new-chat-landing-agent-${pi.id}`)).toBeNull();
    expect(screen.getByTestId("new-chat-landing-harness-more")).toBeInTheDocument();
  });

  it("backfills the inline list when no preferred harness is seeded", async () => {
    // A deployment trimmed to harnesses outside PRIMARY_HARNESS_ORDER. Without
    // the backfill the "Harnesses" group renders empty and every choice hides
    // inside "Other...".
    renderPicker({ harnessEntries: [pi], effectiveAgentId: null });
    await openPicker();

    expect(screen.getByTestId(`new-chat-landing-agent-${pi.id}`)).toBeInTheDocument();
    expect(screen.queryByTestId("new-chat-landing-harness-more")).toBeNull();
  });

  it("backfills at most the usual inline count, leaving the rest under Other", async () => {
    renderPicker({ harnessEntries: [pi, openCode, kiro, polly], effectiveAgentId: null });
    await openPicker();

    // Three promoted inline; the fourth harness stays in the submenu.
    const inline = inlineHarnessIds([pi.id, openCode.id, kiro.id, polly.id]);
    expect(inline).toHaveLength(3);
    expect(screen.getByTestId("new-chat-landing-harness-more")).toBeInTheDocument();
  });

  it("renders no Harnesses group when there are no harnesses at all", async () => {
    renderPicker({ harnessEntries: [], agentEntries: [polly] });
    await openPicker();

    expect(screen.queryByText("Harnesses")).toBeNull();
    expect(screen.getByText("Agents")).toBeInTheDocument();
  });
});

describe("AgentHarnessPicker Agents group header", () => {
  it("renders the header when a bundle agent is present", async () => {
    renderPicker({ harnessEntries: [claude], agentEntries: [polly] });
    await openPicker();

    expect(screen.getByText("Agents")).toBeInTheDocument();
  });

  it("omits the header when there is nothing to put under it", async () => {
    // Trimmed deployment, no custom agents, and no create action (a managed
    // sandbox has no upload path) — a bare "Agents" heading would sit above
    // an empty group.
    renderPicker({
      harnessEntries: [claude],
      agentEntries: [],
      sandboxSelected: true,
      allowCreateCustomAgent: false,
    });
    await openPicker();

    expect(screen.queryByText("Agents")).toBeNull();
    expect(screen.getByText("Harnesses")).toBeInTheDocument();
  });
});
