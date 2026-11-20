import { BotIcon } from "lucide-react";
import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { DevinIcon } from "@/components/icons/DevinIcon";
import { GooseIcon } from "@/components/icons/GooseIcon";
import { HermesIcon } from "@/components/icons/HermesIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { KiroIcon } from "@/components/icons/KiroIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import type { ComponentType, SVGProps } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";

/**
 * Pick the glyph for a catalog agent.
 *
 * Named agents win first (nessie runs on the claude-sdk harness, so a
 * harness check would mislabel it with the Claude glyph), then harness/kind
 * so any Claude-, Codex-, Cursor-, pi-, or Goose-backed agent (native TUI or
 * headless) gets the right glyph regardless of its registered name, then a
 * generic bot (qwen falls back to bot for now).
 *
 * @param agent - The catalog entry to render.
 * @returns The icon component to render for the agent.
 */
export function iconForAgent(
  agent: Pick<AvailableAgent, "name" | "harness">,
): ComponentType<SVGProps<SVGSVGElement>> {
  if (agent.name === "nessie") return NessieIcon;
  const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
  if (nativeAgent?.iconKind === "claude") return ClaudeIcon;
  if (nativeAgent?.iconKind === "codex") return CodexIcon;
  if (nativeAgent?.iconKind === "opencode") return OpenCodeIcon;
  if (nativeAgent?.iconKind === "pi") return PiIcon;
  if (nativeAgent?.iconKind === "cursor") return CursorIcon;
  if (nativeAgent?.iconKind === "kiro") return KiroIcon;
  if (nativeAgent?.iconKind === "goose") return GooseIcon;
  if (nativeAgent?.iconKind === "kimi") return KimiIcon;
  if (nativeAgent?.iconKind === "antigravity") return AntigravityIcon;
  if (nativeAgent?.iconKind === "hermes") return HermesIcon;
  if (nativeAgent?.iconKind === "devin") return DevinIcon;
  // A null harness (spec couldn't load) flows through to the bot fallback.
  if (agent.harness?.includes("codex")) return CodexIcon;
  if (agent.harness?.includes("claude")) return ClaudeIcon;
  // Both the SDK "cursor" harness and "cursor-native" get the Cursor glyph.
  if (agent.harness?.includes("cursor")) return CursorIcon;
  if (agent.harness?.includes("hermes")) return HermesIcon;
  if (agent.harness?.includes("kiro")) return KiroIcon;
  if (agent.harness?.includes("goose")) return GooseIcon;
  // Covers `devin-native`, the retired `devin-acp` id, and a user-configured
  // `acp:devin`.
  if (agent.harness?.includes("devin")) return DevinIcon;
  // Both the SDK "kimi"/"kimi-code" harness and "kimi-native" get the Kimi glyph.
  if (agent.harness?.includes("kimi")) return KimiIcon;
  // qwen falls back to generic BotIcon for now; see docs/QWEN_FOLLOWUPS.md
  // Exact match — a substring check would false-match e.g. "openapi".
  if (agent.harness === "pi") return PiIcon;
  // Both the native (`antigravity-native`) and SDK (`antigravity`) harnesses
  // share the Antigravity glyph.
  if (agent.harness?.includes("antigravity")) return AntigravityIcon;
  return BotIcon;
}
