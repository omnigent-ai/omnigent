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
import { describe, expect, it } from "vitest";
import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { HermesIcon } from "@/components/icons/HermesIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { resolveAgentIcon } from "./subagentIcons";

describe("resolveAgentIcon", () => {
  it("uses a brand icon for a full native child wrapper", () => {
    expect(resolveAgentIcon({ kind: "child", wrapper: "codex-native-ui", tool: "reviewer" })).toBe(
      CodexIcon,
    );
  });

  it("falls back to BotIcon for a root with an unknown wrapper and harness", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: "unknown-wrapper",
        harness: "agents_sdk",
        agentName: "custom-agent",
      }),
    ).toBe(BotIcon);
  });

  it("uses HermesIcon for a Hermes root", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: "hermes-native-ui",
        harness: "hermes-native",
        agentName: "hermes-native-ui",
      }),
    ).toBe(HermesIcon);
  });

  it.each([
    {
      earlier: "claude",
      later: "codex",
      signals: "Claude harness and Codex wrapper",
      wrapper: "codex-native-ui",
      harness: "claude-native",
      expected: ClaudeIcon,
    },
    {
      earlier: "codex",
      later: "antigravity",
      signals: "Codex harness and Antigravity wrapper",
      wrapper: "antigravity-native-ui",
      harness: "codex-native",
      expected: CodexIcon,
    },
    {
      earlier: "antigravity",
      later: "pi",
      signals: "Antigravity harness and Pi wrapper",
      wrapper: "pi-native-ui",
      harness: "antigravity-native",
      expected: AntigravityIcon,
    },
    {
      earlier: "pi",
      later: "hermes",
      signals: "Hermes harness and Pi wrapper",
      wrapper: "pi-native-ui",
      harness: "hermes-native",
      expected: PiIcon,
    },
    {
      earlier: "pi",
      later: "hermes",
      signals: "Pi harness and Hermes wrapper",
      wrapper: "hermes-native-ui",
      harness: "pi",
      expected: PiIcon,
    },
  ])(
    "prefers $earlier over $later for mixed root signals: $signals",
    ({ wrapper, harness, expected }) => {
      expect(resolveAgentIcon({ kind: "root", wrapper, harness, agentName: "mixed-signals" })).toBe(
        expected,
      );
    },
  );

  it("matches the Pi root harness exactly", () => {
    expect(resolveAgentIcon({ kind: "root", wrapper: null, harness: "pi", agentName: "pi" })).toBe(
      PiIcon,
    );
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "openapi",
        agentName: "spec-generator",
      }),
    ).toBe(BotIcon);
  });

  it("checks root harness brands before the Nessie name", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "claude-sdk",
        agentName: "nessie",
      }),
    ).toBe(ClaudeIcon);
  });

  it("matches OpenCode substrings for root harnesses", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "custom-opencode-harness",
        agentName: "custom-agent",
      }),
    ).toBe(OpenCodeIcon);
  });

  it("prefers Nessie by name over its Claude catalog harness", () => {
    expect(resolveAgentIcon({ kind: "catalog", name: "nessie", harness: "claude-sdk" })).toBe(
      NessieIcon,
    );
  });

  it("prefers Codex over Claude for a hybrid catalog harness", () => {
    expect(
      resolveAgentIcon({
        kind: "catalog",
        name: "custom-agent",
        harness: "claude-codex-hybrid",
      }),
    ).toBe(CodexIcon);
  });

  it("excludes unregistered OpenCode substrings from catalog harness matching", () => {
    expect(
      resolveAgentIcon({
        kind: "catalog",
        name: "custom-agent",
        harness: "custom-opencode-harness",
      }),
    ).toBe(BotIcon);
  });

  it.each([
    ["Explore", SearchIcon],
    ["deep-researcher", BookOpenIcon],
    ["planner", CompassIcon],
    ["architect", CompassIcon],
    ["code-reviewer", ScanSearchIcon],
    ["pr-test-analyzer", FlaskConicalIcon],
    ["frontend_engineer", Code2Icon],
    ["documentation", FileTextIcon],
    ["technical-writer", FileTextIcon],
  ])("falls back from an unrecognized child wrapper to the %s role icon", (tool, expected) => {
    expect(resolveAgentIcon({ kind: "child", wrapper: null, tool })).toBe(expected);
  });

  it.each([null, "general-purpose"])("falls back to OttoIcon for child tool %s", (tool) => {
    expect(resolveAgentIcon({ kind: "child", wrapper: null, tool })).toBe(OttoIcon);
  });

  it("lets -subagent wrappers fall through to role icons", () => {
    expect(
      resolveAgentIcon({
        kind: "child",
        wrapper: "claude-code-native-ui-subagent",
        tool: "Explore",
      }),
    ).toBe(SearchIcon);
    expect(
      resolveAgentIcon({
        kind: "child",
        wrapper: "claude-code-native-ui-subagent",
        tool: "claude",
      }),
    ).toBe(OttoIcon);
  });
});
