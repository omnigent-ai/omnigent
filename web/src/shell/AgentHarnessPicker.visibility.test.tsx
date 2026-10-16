import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentHarnessPicker } from "./NewChatDialog";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import {
  HIDDEN_PICKER_AGENTS_STORAGE_KEY,
  writeHiddenPickerAgents,
} from "@/lib/pickerEntryVisibility";

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
const polly = agent({
  id: "agent-polly",
  name: "polly",
  display_name: "Polly",
  harness: "claude-sdk",
});
const debby = agent({
  id: "agent-debby",
  name: "debby",
  display_name: "Debby",
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
  },
};

function renderPicker(effectiveAgentId: string | null = claude.id) {
  return render(
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter>
        <AgentHarnessPicker
          agentEntries={[polly, debby]}
          harnessEntries={[claude, codex]}
          effectiveAgentId={effectiveAgentId}
          agentLabel="Picker"
          hasAgents
          host={readyHost}
          onSelectAgent={() => undefined}
          pendingAgent={null}
          pendingAgentId="pending-agent"
          onSelectPending={() => undefined}
          onCreateCustomAgent={() => undefined}
          sandboxSelected={false}
        />
      </StoryQueryRouter>
    </CapabilitiesProvider>,
  );
}

const openPicker = () => userEvent.click(screen.getByTestId("new-chat-landing-agent-select"));
const row = (id: string) => screen.queryByTestId(`new-chat-landing-agent-${id}`);

afterEach(() => {
  cleanup();
  window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
});

describe("AgentHarnessPicker per-entry visibility", () => {
  it("shows every entry when nothing is hidden", async () => {
    renderPicker();
    await openPicker();

    expect(row(claude.id)).toBeInTheDocument();
    expect(row(codex.id)).toBeInTheDocument();
    expect(row(polly.id)).toBeInTheDocument();
    expect(row(debby.id)).toBeInTheDocument();
  });

  it("drops a hidden harness row", async () => {
    writeHiddenPickerAgents(new Set([codex.name]));
    renderPicker();
    await openPicker();

    expect(row(claude.id)).toBeInTheDocument();
    expect(row(codex.id)).toBeNull();
  });

  it("drops a hidden bundle agent — the readiness filter never could", async () => {
    // Polly is a claude-sdk agent and perfectly launchable here, so "hide
    // unconfigured harnesses" has no effect on it. This preference does.
    writeHiddenPickerAgents(new Set([polly.name]));
    renderPicker();
    await openPicker();

    expect(row(polly.id)).toBeNull();
    expect(row(debby.id)).toBeInTheDocument();
  });

  it("keeps the selected entry visible even when hidden", async () => {
    // Otherwise the current pick vanishes from its own picker and the trigger
    // labels a row the user cannot see or re-select.
    writeHiddenPickerAgents(new Set([claude.name]));
    renderPicker(claude.id);
    await openPicker();

    expect(row(claude.id)).toBeInTheDocument();
  });

  it("reacts to a change without a remount", async () => {
    // The older hide-unconfigured preference is read once per mount; a
    // per-entry list is toggled far more often, so it must not need a reload.
    renderPicker();
    await openPicker();
    expect(row(codex.id)).toBeInTheDocument();

    // act() only flushes React's re-render; the subscription is what makes the
    // already-mounted picker observe the change at all.
    act(() => writeHiddenPickerAgents(new Set([codex.name])));

    expect(row(codex.id)).toBeNull();
  });
});
