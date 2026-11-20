import type { SVGProps } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { iconForAgent } from "./agentIcon";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";

// The real glyphs are brand SVGs (the @lobehub Claude/Codex icons render
// nothing under jsdom), so stub each icon module with a marker element.
// The assertions then prove which icon iconForAgent *chose* for a given
// agent — i.e. the harness→glyph branching — independent of the real
// glyph internals.
function stub(name: string) {
  return (props: SVGProps<SVGSVGElement>) => <svg data-icon={name} {...props} />;
}

vi.mock("@/components/icons/ClaudeIcon", () => ({ ClaudeIcon: stub("claude") }));
vi.mock("@/components/icons/CodexIcon", () => ({ CodexIcon: stub("codex") }));
vi.mock("@/components/icons/CursorIcon", () => ({ CursorIcon: stub("cursor") }));
vi.mock("@/components/icons/GooseIcon", () => ({ GooseIcon: stub("goose") }));
vi.mock("@/components/icons/KiroIcon", () => ({ KiroIcon: stub("kiro") }));
vi.mock("@/components/icons/NessieIcon", () => ({ NessieIcon: stub("nessie") }));
vi.mock("@/components/icons/OpenCodeIcon", () => ({ OpenCodeIcon: stub("opencode") }));
vi.mock("@/components/icons/PiIcon", () => ({ PiIcon: stub("pi") }));
vi.mock("@/components/icons/AntigravityIcon", () => ({ AntigravityIcon: stub("antigravity") }));
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
  const Icon = iconForAgent(a);
  const { container } = render(<Icon />);
  return container.querySelector("[data-icon]")?.getAttribute("data-icon");
}

afterEach(cleanup);

describe("iconForAgent", () => {
  it.each([
    // The whole point of keying on harness: a custom reviewer named
    // "design-reviewer" must still read as Codex, not fall back to bot.
    { name: "design-reviewer", harness: "codex", expected: "codex" },
    { name: "codex-native-ui", harness: "codex-native", expected: "codex" },
    { name: "opencode-native-ui", harness: "opencode-native", expected: "opencode" },
    { name: "claude-native-ui", harness: "claude-native", expected: "claude" },
    { name: "pi-native-ui", harness: "pi-native", expected: "pi" },
    { name: "cursor-native-ui", harness: "cursor-native", expected: "cursor" },
    { name: "goose-native-ui", harness: "goose-native", expected: "goose" },
    // A goose-harnessed agent also reads as Goose via the harness fallback —
    // both the native TUI ("goose-native") and the headless ACP harness ("goose").
    { name: "x", harness: "goose-native", expected: "goose" },
    { name: "x", harness: "goose", expected: "goose" },
    // The SDK "cursor" harness also reads as Cursor via the harness fallback.
    { name: "x", harness: "cursor", expected: "cursor" },
    // Kiro native + the bare "kiro" harness both read as the Kiro glyph
    // (they previously borrowed Cursor's).
    { name: "kiro-native-ui", harness: "kiro-native", expected: "kiro" },
    { name: "x", harness: "kiro-native", expected: "kiro" },
    { name: "x", harness: "kiro", expected: "kiro" },
    { name: "antigravity-native-ui", harness: "antigravity-native", expected: "antigravity" },
    // The in-process Antigravity SDK harness shares the same glyph.
    { name: "x", harness: "antigravity", expected: "antigravity" },
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
