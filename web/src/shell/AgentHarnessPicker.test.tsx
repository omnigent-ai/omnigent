import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AgentHarnessPicker } from "./NewChatDialog";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { StoryQueryRouter } from "@/storybook/StoryProviders";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeHideUnconfiguredHarnesses } from "@/lib/harnessVisibilityPreferences";
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
  /** The deployment's curated roster, as only the New Chat landing passes it. */
  curatedEntries?: AvailableAgent[];
  entriesFailed?: boolean;
  hasAgents?: boolean;
  host?: Host;
  onPrefetchEntries?: (ids: string[]) => void;
}) {
  return render(
    <CapabilitiesProvider info={FALLBACK_SERVER_INFO}>
      <StoryQueryRouter>
        <TooltipProvider>
          <AgentHarnessPicker
            agentEntries={props.agentEntries ?? []}
            harnessEntries={props.harnessEntries}
            curatedEntries={props.curatedEntries}
            entriesFailed={props.entriesFailed}
            onPrefetchEntries={props.onPrefetchEntries}
            effectiveAgentId={props.effectiveAgentId ?? null}
            agentLabel="Picker"
            hasAgents={props.hasAgents ?? true}
            host={props.host ?? readyHost}
            onSelectAgent={() => undefined}
            pendingAgent={null}
            pendingAgentId="pending-agent"
            onSelectPending={() => undefined}
            onCreateCustomAgent={() => undefined}
            sandboxSelected={props.sandboxSelected ?? false}
            allowCreateCustomAgent={props.allowCreateCustomAgent ?? true}
          />
        </TooltipProvider>
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

const row = (id: string) => screen.queryByTestId(`new-chat-landing-agent-${id}`);

afterEach(() => {
  cleanup();
  window.localStorage.removeItem(HIDDEN_PICKER_AGENTS_STORAGE_KEY);
  writeHideUnconfiguredHarnesses(false);
});

describe("AgentHarnessPicker inline harness list", () => {
  it("keeps the preferred harnesses inline when they are seeded", async () => {
    renderPicker({ harnessEntries: [claude, codex, pi], effectiveAgentId: claude.id });
    await openPicker();

    // claude + codex are preferred and stay inline; pi stays behind "Other...".
    expect(inlineHarnessIds([claude.id, codex.id])).toEqual([claude.id, codex.id]);
    expect(screen.queryByTestId(`new-chat-landing-agent-${pi.id}`)).toBeNull();
    expect(screen.getByTestId("new-chat-landing-harness-more")).toBeInTheDocument();
    // With no curation handed down there is no flat curated list either.
    expect(screen.queryByTestId("new-chat-landing-curated")).toBeNull();
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

it("omits the rule above an Agents group that will not render", async () => {
  // Same leftover as the header: a trimmed deployment with no bundle agents
  // and no custom group would otherwise close the menu with a divider and
  // nothing under it.
  renderPicker({
    harnessEntries: [claude],
    agentEntries: [],
    allowCreateCustomAgent: false,
    sandboxSelected: true,
  });
  await openPicker();

  expect(screen.queryByText("Agents")).toBeNull();
  const menu = screen.getByRole("menu");
  expect(menu.querySelectorAll('[role="separator"]')).toHaveLength(0);
});

// Tests the curated menu shape; landing selection is covered in NewChatDialog.test.tsx.
describe("a curated New Chat picker", () => {
  const curatedLabels = () =>
    [
      ...screen
        .getByTestId("new-chat-landing-curated")
        .querySelectorAll("[data-harness-menu-choice-label]"),
    ].map((node) => node.textContent);

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

    expect(screen.getByText("Harnesses")).toBeVisible();
    // Creating stays reachable, in the submenu that groups custom agents.
    expect(screen.getByTestId("new-chat-landing-custom-agents")).toBeVisible();
  });

  describe("with unconfigured harnesses hidden", () => {
    // Codex is not set up on this host; Claude Code and Polly are.
    const partialHost: Host = {
      ...readyHost,
      configured_harnesses: {
        "claude-native": true,
        "codex-native": "binary-missing",
        "claude-sdk": true,
      },
    };
    afterEach(() => localStorage.clear());

    it("hides an unconfigured harness from the curated list, keeping the order", async () => {
      writeHideUnconfiguredHarnesses(true);
      renderPicker({
        harnessEntries: [claude, codex],
        agentEntries: [polly],
        curatedEntries: [polly, codex, claude],
        host: partialHost,
      });
      await openPicker();

      expect(curatedLabels()).toEqual(["Polly", "Claude Code"]);
    });

    it("keeps the selected harness in the list even when it is unconfigured", async () => {
      writeHideUnconfiguredHarnesses(true);
      renderPicker({
        harnessEntries: [claude, codex],
        agentEntries: [polly],
        curatedEntries: [polly, codex, claude],
        effectiveAgentId: codex.id,
        host: partialHost,
      });
      await openPicker();

      expect(curatedLabels()).toEqual(["Polly", "Codex", "Claude Code"]);
    });

    it("explains an empty list that the preference, not the deployment, emptied", async () => {
      writeHideUnconfiguredHarnesses(true);
      renderPicker({ harnessEntries: [codex], curatedEntries: [codex], host: partialHost });
      await openPicker();

      expect(screen.getByTestId("new-chat-landing-no-agents")).toHaveTextContent(
        "hidden by your settings",
      );
    });
  });

  it("blames the fetch, not the deployment, when the roster could not be loaded", async () => {
    // An agents-fetch failure empties the list too, and the trigger still
    // opens; saying the deployment limits the picker would be wrong.
    renderPicker({ harnessEntries: [], curatedEntries: [], entriesFailed: true });
    await openPicker();

    const empty = screen.getByTestId("new-chat-landing-no-agents");
    expect(empty).toHaveTextContent("Agents could not be loaded.");
    expect(empty).toHaveTextContent("Check your connection and try again.");
    expect(empty).not.toHaveTextContent("This deployment limits");
  });

  it("prefetches sandbox support for every curated harness row it shows", async () => {
    const prefetched: string[][] = [];
    renderPicker({
      harnessEntries: [claude, codex, pi],
      agentEntries: [polly],
      curatedEntries: [polly, pi, codex],
      onPrefetchEntries: (ids) => prefetched.push(ids),
    });
    await openPicker();

    // The harness rows the curated list shows, not the grouped menu's inline set.
    expect(prefetched.at(-1)).toEqual([pi.id, codex.id]);
  });

  describe("with entries the user hid", () => {
    it("drops a hidden entry from the curated list, keeping the order", async () => {
      writeHiddenPickerAgents(new Set([codex.name]));
      renderPicker({
        harnessEntries: [claude, codex],
        agentEntries: [polly],
        curatedEntries: [polly, codex, claude],
      });
      await openPicker();

      expect(curatedLabels()).toEqual(["Polly", "Claude Code"]);
    });

    it("points at Settings when the user's hides emptied the curated list", async () => {
      writeHiddenPickerAgents(new Set([polly.name]));
      renderPicker({ harnessEntries: [claude], agentEntries: [polly], curatedEntries: [polly] });
      await openPicker();

      expect(screen.getByTestId("new-chat-landing-no-agents")).toHaveTextContent(
        "Every entry is hidden.",
      );
    });
  });
});

describe("AgentHarnessPicker per-entry visibility", () => {
  it.each([
    ["harness", codex],
    // Polly launches fine here, so only this preference can drop it.
    ["bundle agent", polly],
  ])("drops a hidden %s row", async (_kind, hiddenEntry) => {
    writeHiddenPickerAgents(new Set([hiddenEntry.name]));
    renderPicker({
      harnessEntries: [claude, codex],
      agentEntries: [polly, debby],
      effectiveAgentId: claude.id,
    });
    await openPicker();

    expect(row(hiddenEntry.id)).toBeNull();
    expect(row(claude.id)).toBeInTheDocument();
    expect(row(debby.id)).toBeInTheDocument();
  });

  it("keeps the selected entry visible even when hidden", async () => {
    writeHiddenPickerAgents(new Set([claude.name]));
    renderPicker({ harnessEntries: [claude, codex], effectiveAgentId: claude.id });
    await openPicker();

    expect(row(claude.id)).toBeInTheDocument();
  });

  it("reacts to a change without a remount", async () => {
    renderPicker({ harnessEntries: [claude, codex], effectiveAgentId: claude.id });
    await openPicker();
    expect(row(codex.id)).toBeInTheDocument();

    act(() => writeHiddenPickerAgents(new Set([codex.name])));

    expect(row(codex.id)).toBeNull();
  });

  const claudeOnlyHost: Host = {
    ...readyHost,
    configured_harnesses: { "claude-native": true, "codex-native": false },
  };
  const unconfiguredHost: Host = {
    ...readyHost,
    configured_harnesses: { "claude-native": false, "codex-native": false },
  };
  it.each([
    ["every offered entry is hidden", [claude.name, codex.name, polly.name], readyHost, true],
    ["a hidden ready harness is all that was left", [claude.name], claudeOnlyHost, true],
    ["only a stale name is stored", ["removed-agent"], unconfiguredHost, false],
    ["readiness already removed the hidden harness", [codex.name], unconfiguredHost, false],
  ])(
    "points at Settings only when the hides emptied the picker: %s",
    async (_case, hiddenNames, host, pointsAtSettings) => {
      writeHiddenPickerAgents(new Set(hiddenNames));
      writeHideUnconfiguredHarnesses(true);
      renderPicker({
        harnessEntries: [claude, codex],
        agentEntries: host === readyHost ? [polly] : [],
        effectiveAgentId: null,
        host,
      });
      await openPicker();

      expect(row(claude.id)).toBeNull();
      expect(row(codex.id)).toBeNull();
      expect(screen.queryByTestId("new-chat-landing-all-hidden") !== null).toBe(pointsAtSettings);
    },
  );
});
