// Canonical agent-icon resolution shared by the Agents rail's list view
// (SubagentsPanel), its graph view (SubagentsGraphView), and the agent
// catalog cards (AgentCard), so the three never drift. Mirrors the split in
// ``subagentStatus.ts``: one module owns the mapping, every view reads it.
//
// Three tiers, in order:
//   1. Brand glyph — a native wrapper label, agent name, or harness
//      identifies the harness running the session (Claude, Codex, pi, …).
//   2. Role icon — no brand match, so the sub-agent *type* (``tool``)
//      picks a category glyph (Explore → magnifier, reviewer → scan, …).
//   3. Fallback — Otto for typed sub-agent children, ``BotIcon`` for root
//      and catalog rows, which are whole sessions with no role to read.

import type { ComponentType, SVGProps } from "react";
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
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
  nativeCodingAgentForWrapper,
  WRAPPER_LABEL_KEY,
  type NativeCodingAgentIconKind,
} from "@/lib/nativeCodingAgents";

export type AgentRowIcon = ComponentType<SVGProps<SVGSVGElement>>;

/** Classes every agent icon in the Agents rail renders with, so the list
 *  and graph views size and tint the glyph identically. */
export const AGENT_ICON_CLASS = "size-3.5 shrink-0 text-muted-foreground";

// Brand glyph per ``iconKind``. ``qwen`` is absent on purpose — it has no
// brand mark yet, so it falls through to the bot / role icons.
const BRAND_ICONS: Partial<Record<NativeCodingAgentIconKind, AgentRowIcon>> = {
  claude: ClaudeIcon,
  codex: CodexIcon,
  opencode: OpenCodeIcon,
  pi: PiIcon,
  cursor: CursorIcon,
  kiro: KiroIcon,
  antigravity: AntigravityIcon,
  goose: GooseIcon,
  kimi: KimiIcon,
  hermes: HermesIcon,
};

// Harness spellings matched as substrings so both forms of a harness
// (``claude-native`` and ``claude-sdk``) land on the same glyph. ``pi`` is
// excluded — it is matched exactly, since a substring check would
// false-match names like "openapi".
const HARNESS_SUBSTRINGS: readonly NativeCodingAgentIconKind[] = [
  "claude",
  "codex",
  "opencode",
  "cursor",
  "kiro",
  "goose",
  "kimi",
  "antigravity",
  "hermes",
];

// Wrapper suffix marking a *sub-agent* of a native session rather than the
// native session itself. These deliberately get role icons: a native
// session's sub-agents are all the same brand, so repeating the logo down
// the tree says nothing, while role icons distinguish what each is doing.
const NATIVE_SUBAGENT_WRAPPER_SUFFIX = "-subagent";

// Nessie runs on the claude-sdk harness, so it is matched by name before
// any harness check — otherwise it would wear the Claude glyph.
const NESSIE_AGENT_NAME = "nessie";

// Pi scaffold children carry no wrapper label, so the spawn title's
// agent-type head (``tool``) is the only identity signal.
const PI_AGENT_NAME = "pi";

function brandForIconKind(kind: NativeCodingAgentIconKind | undefined): AgentRowIcon | null {
  return (kind && BRAND_ICONS[kind]) ?? null;
}

/**
 * Resolve a brand glyph from a native wrapper label, agent name, or harness.
 *
 * The wrapper label and agent name are authoritative exact lookups — a
 * custom scaffold agent merely *named* "codex" must not borrow the Codex
 * logo. The harness substring pass then covers plain SDK sessions that
 * carry no wrapper label, e.g. ``omni --harness kimi``.
 *
 * @param source - Whichever identity signals the caller has.
 * @returns The brand glyph, or ``null`` when nothing matches.
 */
export function brandIconFor({
  wrapper,
  agentName,
  harness,
}: {
  wrapper?: string | null;
  agentName?: string | null;
  harness?: string | null;
}): AgentRowIcon | null {
  const byWrapper = brandForIconKind(nativeCodingAgentForWrapper(wrapper)?.iconKind);
  if (byWrapper) return byWrapper;
  const byName = brandForIconKind(nativeCodingAgentForAgentName(agentName)?.iconKind);
  if (byName) return byName;
  const byHarness = brandForIconKind(nativeCodingAgentForHarness(harness)?.iconKind);
  if (byHarness) return byHarness;
  if (harness) {
    for (const kind of HARNESS_SUBSTRINGS) {
      if (harness.includes(kind)) return BRAND_ICONS[kind] ?? null;
    }
    // Exact match — a substring check would false-match e.g. "openapi".
    if (harness === PI_AGENT_NAME) return PiIcon;
  }
  return null;
}

/**
 * Map a sub-agent type label to a category icon so a mix of agents reads by
 * role at a glance (Claude Code spawns many same-type "Explore" agents — the
 * icon distinguishes roles; the preview line below distinguishes instances).
 * Category icons are monochrome — the caller applies the muted color; the
 * fallback is the full-color Otto (starfish) mascot.
 *
 * @param tool - The agent type, e.g. ``"Explore"`` or ``"researcher"``;
 *   ``null`` when the child carries no type.
 * @returns An SVG icon component.
 */
export function iconForAgentType(tool: string | null | undefined): AgentRowIcon {
  const t = (tool ?? "").toLowerCase();
  if (t.includes("explore")) return SearchIcon;
  if (t.includes("research")) return BookOpenIcon;
  if (t.includes("plan") || t.includes("architect")) return CompassIcon;
  if (t.includes("review")) return ScanSearchIcon;
  if (t.includes("test")) return FlaskConicalIcon;
  if (t.includes("doc") || t.includes("writ")) return FileTextIcon;
  if (
    t.includes("code") ||
    t.includes("eng") ||
    t.includes("dev") ||
    t.includes("front") ||
    t.includes("back")
  ) {
    return Code2Icon;
  }
  return OttoIcon;
}

/**
 * Icon for one child (sub-agent) row: brand glyph when the child is a full
 * native session, else a role icon from its type, else Otto.
 *
 * A child's ``tool`` is a sub-agent *type*, never a harness, so it is not
 * substring-matched against brand names — only the exact ``"pi"`` scaffold
 * name is, since pi children carry no wrapper label.
 *
 * @param child - The child's identity fields, as raw labels or a
 *   pre-extracted ``wrapper``.
 * @returns The glyph to render on the row.
 */
export function iconForChildAgent(child: {
  labels?: Record<string, string> | null;
  wrapper?: string | null;
  tool?: string | null;
}): AgentRowIcon {
  const wrapper = child.wrapper ?? child.labels?.[WRAPPER_LABEL_KEY];
  // Sub-agents of a native session read by role, not by brand. Redundant
  // with the wrapper lookup missing today, but states the rule where the
  // decision is made rather than leaving it to a map miss.
  if (wrapper?.endsWith(NATIVE_SUBAGENT_WRAPPER_SUFFIX)) return iconForAgentType(child.tool);
  const brand = brandIconFor({ wrapper });
  if (brand) return brand;
  // Exact match — substring checks would false-match names like "pipeline".
  if (child.tool === PI_AGENT_NAME) return PiIcon;
  return iconForAgentType(child.tool);
}

/**
 * Icon for a root or catalog row, which represents a whole session rather
 * than a typed sub-agent: brand glyph, then the nessie mascot, then a
 * generic bot — there is no role ``tool`` to fall back on.
 *
 * @param session - The session's wrapper label, agent name, and harness.
 * @returns The glyph to render.
 */
export function iconForSessionAgent({
  wrapper,
  agentName,
  harness,
}: {
  wrapper?: string | null;
  agentName?: string | null;
  harness?: string | null;
}): AgentRowIcon {
  // Exact wrapper / agent-name lookups first, then nessie by name, then
  // the fuzzy harness pass — nessie is on claude-sdk, so a harness-first
  // check would hand it the Claude glyph.
  const exact = brandIconFor({ wrapper, agentName });
  if (exact) return exact;
  if (agentName === NESSIE_AGENT_NAME) return NessieIcon;
  return brandIconFor({ harness }) ?? BotIcon;
}
