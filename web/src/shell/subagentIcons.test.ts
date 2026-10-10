import { BotIcon, SearchIcon } from "lucide-react";
import { describe, expect, it } from "vitest";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { HermesIcon } from "@/components/icons/HermesIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import { resolveSubagentIcon } from "./subagentIcons";

describe("resolveSubagentIcon", () => {
  it("resolves a branded root through its wrapper identity", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: "codex-native-ui",
        harness: null,
        agentName: null,
      }),
    ).toBe(CodexIcon);
  });

  it("keeps the Nessie icon when its root runs on a Claude harness", () => {
    // Nessie is the product identity even though it runs on Claude's harness.
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: null,
        harness: "claude-sdk",
        agentName: "nessie",
      }),
    ).toBe(NessieIcon);
  });

  it("keeps the Nessie icon when its root runs on an OpenCode harness", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: null,
        harness: "opencode",
        agentName: "nessie",
      }),
    ).toBe(NessieIcon);
  });

  it("recognizes Hermes roots through the shared catalog resolver", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: "hermes-native-ui",
        harness: "hermes-native",
        agentName: "hermes-native-ui",
      }),
    ).toBe(HermesIcon);
  });

  it("recognizes wrapper-less OpenCode root harnesses", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: null,
        harness: "opencode",
        agentName: "custom-agent",
      }),
    ).toBe(OpenCodeIcon);
  });

  it("does not infer a root brand from agentName alone", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: null,
        harness: "agents-sdk",
        agentName: "codex-native-ui",
      }),
    ).toBe(BotIcon);
  });

  it("uses a brand icon for a full native child wrapper", () => {
    expect(
      resolveSubagentIcon({ kind: "child", wrapper: "codex-native-ui", tool: "reviewer" }),
    ).toBe(CodexIcon);
  });

  it("uses a role icon for native sub-agent children", () => {
    expect(
      resolveSubagentIcon({
        kind: "child",
        wrapper: "codex-native-ui-subagent",
        tool: "Explore",
      }),
    ).toBe(SearchIcon);
  });

  it("falls through to a role icon when a child wrapper has no recognized brand", () => {
    expect(
      resolveSubagentIcon({
        kind: "child",
        wrapper: "qwen-native-ui",
        tool: "Explore",
      }),
    ).toBe(SearchIcon);
  });

  it("uses the Pi brand only for the exact pi child tool", () => {
    expect(resolveSubagentIcon({ kind: "child", wrapper: null, tool: "pi" })).toBe(PiIcon);
  });

  it("does not brand child tools that merely contain pi", () => {
    expect(resolveSubagentIcon({ kind: "child", wrapper: null, tool: "pipeline" })).toBe(OttoIcon);
  });

  it("falls back for unknown root and child identities", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: "unknown-wrapper",
        harness: "agents-sdk",
        agentName: "custom-agent",
      }),
    ).toBe(BotIcon);
    expect(resolveSubagentIcon({ kind: "child", wrapper: null, tool: "general-purpose" })).toBe(
      OttoIcon,
    );
  });
});
