import type { ComponentType, SVGProps } from "react";
import {
  BookOpenIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { iconForAgent } from "@/components/AgentCard";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { nativeCodingAgentForWrapper } from "@/lib/nativeCodingAgents";

export type AgentIcon = ComponentType<SVGProps<SVGSVGElement>>;

export type SubagentIconSource =
  | {
      kind: "root";
      wrapper: string | null;
      harness: string | null;
      agentName: string | null;
    }
  | {
      kind: "child";
      wrapper: string | null;
      tool: string | null;
    };

const PI_AGENT_NAME = "pi";
const UNBRANDED_ICON = iconForAgent({ name: "", harness: null });

/** Map child tool names to role glyphs, with Otto for unknown roles. */
export function iconForAgentType(tool: string | null): AgentIcon {
  const normalized = (tool ?? "").toLowerCase();
  if (normalized.includes("explore")) return SearchIcon;
  if (normalized.includes("research")) return BookOpenIcon;
  if (normalized.includes("plan") || normalized.includes("architect")) return CompassIcon;
  if (normalized.includes("review")) return ScanSearchIcon;
  if (normalized.includes("test")) return FlaskConicalIcon;
  if (normalized.includes("doc") || normalized.includes("writ")) return FileTextIcon;
  if (
    normalized.includes("code") ||
    normalized.includes("eng") ||
    normalized.includes("dev") ||
    normalized.includes("front") ||
    normalized.includes("back")
  ) {
    return Code2Icon;
  }
  return OttoIcon;
}

/** Resolve via BY_WRAPPER; BY_SUBAGENT_WRAPPER labels deliberately fall through to roles. */
export function resolveSubagentIcon(source: SubagentIconSource): AgentIcon {
  const nativeAgent = nativeCodingAgentForWrapper(source.wrapper);
  if (source.kind === "root") {
    const icon = iconForAgent({
      name: source.agentName === "nessie" ? source.agentName : (nativeAgent?.agentName ?? ""),
      harness: nativeAgent?.harness ?? source.harness,
    });
    if (icon === UNBRANDED_ICON && source.harness?.includes("opencode")) return OpenCodeIcon;
    return icon;
  }
  if (nativeAgent !== undefined) {
    const icon = iconForAgent({ name: nativeAgent.agentName, harness: nativeAgent.harness });
    // A generic fallback means the wrapper has no branded glyph, so preserve the role icon.
    if (icon !== UNBRANDED_ICON) return icon;
  }
  if (source.tool === PI_AGENT_NAME) return iconForAgent({ name: "", harness: PI_AGENT_NAME });
  return iconForAgentType(source.tool);
}
