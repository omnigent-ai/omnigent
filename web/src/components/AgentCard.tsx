import { BotIcon } from "lucide-react";
import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { GooseIcon } from "@/components/icons/GooseIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import type { ComponentType, SVGProps } from "react";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { AgentHoverCard } from "@/components/AgentHoverCard";

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
function iconForAgent(agent: AvailableAgent): ComponentType<SVGProps<SVGSVGElement>> {
  if (agent.name === "nessie") return NessieIcon;
  const nativeAgent = nativeCodingAgentForAvailableAgent(agent);
  if (nativeAgent?.iconKind === "claude") return ClaudeIcon;
  if (nativeAgent?.iconKind === "codex") return CodexIcon;
  if (nativeAgent?.iconKind === "opencode") return OpenCodeIcon;
  if (nativeAgent?.iconKind === "pi") return PiIcon;
  if (nativeAgent?.iconKind === "cursor") return CursorIcon;
  if (nativeAgent?.iconKind === "kiro") return CursorIcon;
  if (nativeAgent?.iconKind === "goose") return GooseIcon;
  if (nativeAgent?.iconKind === "kimi") return KimiIcon;
  if (nativeAgent?.iconKind === "antigravity") return AntigravityIcon;
  // A null harness (spec couldn't load) flows through to the bot fallback.
  if (agent.harness?.includes("codex")) return CodexIcon;
  if (agent.harness?.includes("claude")) return ClaudeIcon;
  // Both the SDK "cursor" harness and "cursor-native" get the Cursor glyph.
  if (agent.harness?.includes("cursor")) return CursorIcon;
  if (agent.harness?.includes("kiro")) return CursorIcon;
  if (agent.harness?.includes("goose")) return GooseIcon;
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

/**
 * Selectable card for one available agent.
 *
 * Shared by the new-session picker (NewChatDialog) and the "Add agent"
 * picker (AddAgentDialog) so both render the agent catalog identically.
 * Claude, Codex, and pi agents reuse their own glyphs; qwen falls back
 * to a generic bot icon for now. Nessie matches by name. Everything else
 * falls back to a generic bot icon.
 *
 * @param agent - The catalog entry to render.
 * @param selected - Whether this card is the current selection.
 * @param onSelect - Invoked when the card is clicked.
 * @param compact - When true, render icon + name only (no inline
 *   description) so cards stay even in a horizontal row; the
 *   description is surfaced as a hover tooltip instead.
 * @param hover - When true, wrap the card in a Cursor-style hover
 *   flyout (``AgentHoverCard``) that opens to the right with the
 *   agent's name + description. Additive to the inline description.
 *   Ignored in compact mode, which already surfaces the description
 *   via its own tooltip.
 */
export function AgentCard({
  agent,
  selected,
  onSelect,
  compact = false,
  hover = false,
}: {
  agent: AvailableAgent;
  selected: boolean;
  onSelect: () => void;
  compact?: boolean;
  hover?: boolean;
}) {
  const Icon = iconForAgent(agent);
  const card = (
    <button
      type="button"
      data-testid={`agent-card-${agent.id}`}
      onClick={onSelect}
      className={`flex w-full items-center gap-3 rounded-lg border p-3 text-left transition ${
        selected ? "border-primary bg-primary/5" : "border-border hover:border-muted-foreground/30"
      } cursor-pointer`}
    >
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <span className="text-xs font-semibold">{agent.display_name}</span>
        {!compact && agent.description && (
          <p className="mt-0.5 text-xs text-muted-foreground">{agent.description}</p>
        )}
      </div>
    </button>
  );

  // Compact cards drop the inline description to keep heights even in a
  // row; surface it via a tooltip. Use the component tooltip (≈500ms
  // open) instead of the native ``title`` attribute, whose multi-second
  // browser-imposed delay feels broken.
  if (compact && agent.description) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>{card}</TooltipTrigger>
        <TooltipContent>{agent.description}</TooltipContent>
      </Tooltip>
    );
  }
  // Non-compact opt-in: surface the richer Cursor-style flyout to the
  // right on hover. AgentHoverCard no-ops when there's no description.
  if (hover) {
    return <AgentHoverCard agent={agent}>{card}</AgentHoverCard>;
  }
  return card;
}
