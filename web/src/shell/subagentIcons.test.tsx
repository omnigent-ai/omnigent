import { describe, expect, it, vi } from "vitest";
import {
  BookOpenIcon,
  BotIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";

// Stub the brand logos so jsdom doesn't resolve @lobehub/ui's runtime
// tooltip imports; the identity comparisons below don't need real SVGs.
vi.mock("@/components/icons/ClaudeIcon", () => ({ ClaudeIcon: () => null }));
vi.mock("@/components/icons/CodexIcon", () => ({ CodexIcon: () => null }));
vi.mock("@/components/icons/OpenCodeIcon", () => ({ OpenCodeIcon: () => null }));
vi.mock("@/components/icons/CursorIcon", () => ({ CursorIcon: () => null }));
vi.mock("@/components/icons/KiroIcon", () => ({ KiroIcon: () => null }));
vi.mock("@/components/icons/GooseIcon", () => ({ GooseIcon: () => null }));
vi.mock("@/components/icons/KimiIcon", () => ({ KimiIcon: () => null }));
vi.mock("@/components/icons/HermesIcon", () => ({ HermesIcon: () => null }));
vi.mock("@/components/icons/AntigravityIcon", () => ({ AntigravityIcon: () => null }));
vi.mock("@/components/icons/NessieIcon", () => ({ NessieIcon: () => null }));
vi.mock("@/components/icons/PiIcon", () => ({ PiIcon: () => null }));
vi.mock("@/components/icons/OttoIcon", () => ({ OttoIcon: () => null }));

import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { GooseIcon } from "@/components/icons/GooseIcon";
import { HermesIcon } from "@/components/icons/HermesIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { KiroIcon } from "@/components/icons/KiroIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import {
  iconForAgentType,
  iconForChildAgent,
  iconForSessionAgent,
  type AgentRowIcon,
} from "./subagentIcons";

// ===========================================================================
// Tier 2: role icons from the sub-agent type
// ===========================================================================

describe("iconForAgentType", () => {
  // Order-sensitive cases (review before code, doc before writ) are covered
  // by the multi-keyword entries below.
  const CASES: [string | null, AgentRowIcon][] = [
    ["Explore", SearchIcon],
    ["deep-researcher", BookOpenIcon],
    ["planner", CompassIcon],
    ["architect", CompassIcon],
    ["code-reviewer", ScanSearchIcon],
    ["pr-test-analyzer", FlaskConicalIcon],
    ["frontend_engineer", Code2Icon],
    ["documentation", FileTextIcon],
    ["technical-writer", FileTextIcon],
    ["general-purpose", OttoIcon],
    [null, OttoIcon],
  ];

  it.each(CASES)("maps %s to its category icon", (tool, expected) => {
    expect(iconForAgentType(tool)).toBe(expected);
  });

  it("falls back to Otto for an undefined type", () => {
    expect(iconForAgentType(undefined)).toBe(OttoIcon);
  });
});

// ===========================================================================
// Child rows: brand → role → Otto
// ===========================================================================

describe("iconForChildAgent", () => {
  const WRAPPER_BRANDS: [string, AgentRowIcon][] = [
    ["claude-code-native-ui", ClaudeIcon],
    ["codex-native-ui", CodexIcon],
    ["opencode-native-ui", OpenCodeIcon],
    ["cursor-native-ui", CursorIcon],
    ["kiro-native-ui", KiroIcon],
    ["pi-native-ui", PiIcon],
    ["goose-native-ui", GooseIcon],
    ["kimi-native-ui", KimiIcon],
    ["hermes-native-ui", HermesIcon],
    ["antigravity-native-ui", AntigravityIcon],
  ];

  it.each(WRAPPER_BRANDS)("gives %s children its brand glyph", (wrapper, expected) => {
    expect(iconForChildAgent({ wrapper, tool: "Explore" })).toBe(expected);
  });

  it("reads the wrapper off raw session labels too", () => {
    expect(iconForChildAgent({ labels: { "omnigent.wrapper": "codex-native-ui" } })).toBe(
      CodexIcon,
    );
  });

  // Tier 3 of the deliberate design rule: a native session's sub-agents are
  // all the same brand, so `…-subagent` wrappers read by ROLE, not brand.
  it.each(WRAPPER_BRANDS.map(([wrapper]) => wrapper))(
    "falls through to role icons for the %s-subagent wrapper",
    (wrapper) => {
      expect(iconForChildAgent({ wrapper: `${wrapper}-subagent`, tool: "Explore" })).toBe(
        SearchIcon,
      );
    },
  );

  it("gives -subagent children with no usable type the Otto fallback", () => {
    expect(iconForChildAgent({ wrapper: "claude-code-native-ui-subagent", tool: "claude" })).toBe(
      OttoIcon,
    );
  });

  it("does not infer a brand from the tool name alone", () => {
    // A custom scaffold agent merely NAMED "codex" must not borrow the logo.
    // It reads as a role instead ("codex" contains "code" → Code2Icon).
    expect(iconForChildAgent({ tool: "codex" })).toBe(Code2Icon);
    expect(iconForChildAgent({ tool: "claude" })).toBe(OttoIcon);
  });

  it("uses the pi glyph for pi children, matched by exact tool name", () => {
    expect(iconForChildAgent({ tool: "pi" })).toBe(PiIcon);
  });

  it("does not match pi as a substring of a longer tool name", () => {
    expect(iconForChildAgent({ tool: "pipeline" })).not.toBe(PiIcon);
  });

  it("falls back to a role icon for an unknown wrapper", () => {
    expect(iconForChildAgent({ wrapper: "some-other-wrapper", tool: "reviewer" })).toBe(
      ScanSearchIcon,
    );
  });

  it("falls back to Otto when there is no wrapper and no usable type", () => {
    expect(iconForChildAgent({})).toBe(OttoIcon);
  });

  it("gives qwen children a role icon — it has no brand glyph yet", () => {
    expect(iconForChildAgent({ wrapper: "qwen-native-ui", tool: "Explore" })).toBe(SearchIcon);
  });
});

// ===========================================================================
// Root / catalog rows: brand → nessie → bot (no role tier)
// ===========================================================================

describe("iconForSessionAgent", () => {
  it.each([
    ["claude-code-native-ui", ClaudeIcon],
    ["codex-native-ui", CodexIcon],
    ["opencode-native-ui", OpenCodeIcon],
    ["hermes-native-ui", HermesIcon],
  ] as [string, AgentRowIcon][])("resolves the %s wrapper to its glyph", (wrapper, expected) => {
    expect(iconForSessionAgent({ wrapper })).toBe(expected);
  });

  it.each([
    ["claude-sdk", ClaudeIcon],
    ["codex-native", CodexIcon],
    ["opencode", OpenCodeIcon],
    ["cursor", CursorIcon],
    ["kimi-code", KimiIcon],
    ["hermes", HermesIcon],
    ["antigravity", AntigravityIcon],
    ["goose", GooseIcon],
    ["kiro", KiroIcon],
  ] as [string, AgentRowIcon][])(
    "falls back to the %s harness when there is no wrapper label",
    (harness, expected) => {
      expect(iconForSessionAgent({ harness })).toBe(expected);
    },
  );

  it("matches the pi harness exactly, not as a substring", () => {
    expect(iconForSessionAgent({ harness: "pi" })).toBe(PiIcon);
    expect(iconForSessionAgent({ harness: "openapi" })).toBe(BotIcon);
  });

  it("resolves nessie by name even on the claude-sdk harness", () => {
    // A harness-first check would hand nessie the Claude glyph.
    expect(iconForSessionAgent({ agentName: "nessie", harness: "claude-sdk" })).toBe(NessieIcon);
  });

  it("resolves nessie by name when the harness is null", () => {
    expect(iconForSessionAgent({ agentName: "nessie", harness: null })).toBe(NessieIcon);
  });

  it("resolves a native agent name when no wrapper or harness is present", () => {
    expect(iconForSessionAgent({ agentName: "codex-native-ui" })).toBe(CodexIcon);
  });

  // Tier 1 of the fallback contract: root rows with nothing recognizable
  // get the generic bot, NOT Otto — they are sessions, not typed sub-agents.
  it("falls back to the generic bot for an unknown wrapper and harness", () => {
    expect(iconForSessionAgent({ wrapper: "some-other-wrapper", harness: "mystery" })).toBe(
      BotIcon,
    );
  });

  it("falls back to the generic bot when every signal is absent", () => {
    expect(iconForSessionAgent({})).toBe(BotIcon);
  });

  it("falls back to the generic bot for qwen, which has no brand glyph yet", () => {
    expect(iconForSessionAgent({ wrapper: "qwen-native-ui" })).toBe(BotIcon);
  });
});
