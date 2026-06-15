import type { SVGProps } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentCard } from "./AgentCard";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

// The real glyphs are brand SVGs (the @lobehub Claude/Codex icons render
// nothing under jsdom), so stub each icon module with a marker element.
// The assertions then prove which icon AgentCard *chose* for a given
// agent — i.e. the harness→glyph branching — independent of the real
// glyph internals.
function stub(name: string) {
  return (props: SVGProps<SVGSVGElement>) => <svg data-icon={name} {...props} />;
}

vi.mock("@/components/icons/ClaudeIcon", () => ({ ClaudeIcon: stub("claude") }));
vi.mock("@/components/icons/CodexIcon", () => ({ CodexIcon: stub("codex") }));
vi.mock("@/components/icons/NessieIcon", () => ({ NessieIcon: stub("nessie") }));
vi.mock("@/components/icons/PiIcon", () => ({ PiIcon: stub("pi") }));
vi.mock("lucide-react", () => ({ BotIcon: stub("bot") }));

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_1",
    name: "some-agent",
    display_name: "Some Agent",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

function chosenIcon(a: AvailableAgent): string | null | undefined {
  const { container } = render(<AgentCard agent={a} selected={false} onSelect={() => {}} />);
  return container.querySelector("[data-icon]")?.getAttribute("data-icon");
}

afterEach(cleanup);

describe("AgentCard icon selection", () => {
  it.each([
    // The whole point of keying on harness: a custom reviewer named
    // "design-reviewer" must still read as Codex, not fall back to bot.
    { name: "design-reviewer", harness: "codex", expected: "codex" },
    { name: "codex-native-ui", harness: "codex-native", expected: "codex" },
    { name: "claude-native-ui", harness: "claude-native", expected: "claude" },
    { name: "x", harness: "claude-sdk", expected: "claude" },
    { name: "pi", harness: "pi", expected: "pi" },
    // The pi match is exact: a harness merely containing "pi" stays generic.
    { name: "spec-gen", harness: "openapi", expected: "bot" },
  ])("uses the $expected glyph for harness $harness", ({ name, harness, expected }) => {
    expect(chosenIcon(agent({ name, harness }))).toBe(expected);
  });

  it("uses the nessie glyph by name even on the claude-sdk harness", () => {
    // nessie runs on claude-sdk, so a harness-first check would mislabel
    // it as Claude. The name match must win.
    expect(chosenIcon(agent({ name: "nessie", harness: "claude-sdk" }))).toBe("nessie");
  });

  it("uses the nessie glyph by name when harness is null", () => {
    expect(chosenIcon(agent({ name: "nessie", harness: null }))).toBe("nessie");
  });

  it("falls back to the generic bot glyph for an unknown agent", () => {
    // Neither the codex/claude harness match nor the nessie name match
    // fires, so the generic bot is the floor.
    expect(chosenIcon(agent({ name: "mystery", harness: "agents_sdk" }))).toBe("bot");
  });
});

describe("AgentCard compact mode", () => {
  const withDescription = agent({
    display_name: "Nessie",
    description: "Multi-agent coding orchestrator.",
  });

  it("hides the inline description and moves it into a tooltip", () => {
    // Compact cards sit in a horizontal row, so the long description is
    // dropped from the body (to keep heights even) and shown on hover
    // via the tooltip instead.
    render(
      <TooltipProvider>
        <AgentCard agent={withDescription} selected={false} onSelect={() => {}} compact />
      </TooltipProvider>,
    );
    const card = screen.getByTestId("agent-card-ag_1");
    expect(card).toHaveTextContent("Nessie"); // name still shown
    // Description is NOT inline — it lives in the (closed) tooltip
    // content, absent from the DOM until hover/focus. If compact stopped
    // hiding it, this query would find the inline text and fail.
    expect(screen.queryByText("Multi-agent coding orchestrator.")).toBeNull();
    // The card itself is the tooltip trigger (asChild merges the slot
    // marker onto the button); losing the wrapper drops the hover text.
    expect(card).toHaveAttribute("data-slot", "tooltip-trigger");
  });

  it("renders the description inline with no tooltip in the default mode", () => {
    render(<AgentCard agent={withDescription} selected={false} onSelect={() => {}} />);
    const card = screen.getByTestId("agent-card-ag_1");
    // Non-compact (AddAgentDialog) keeps the full card: description
    // inline, and the card is not wrapped as a tooltip trigger.
    expect(card).toHaveTextContent("Multi-agent coding orchestrator.");
    expect(card).not.toHaveAttribute("data-slot", "tooltip-trigger");
  });
});
