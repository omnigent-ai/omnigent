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
  nativeCodingAgentForAvailableAgent,
  nativeCodingAgentForWrapper,
  type NativeCodingAgentIconKind,
} from "@/lib/nativeCodingAgents";

export type AgentIcon = ComponentType<SVGProps<SVGSVGElement>>;

export type AgentIconSource =
  | {
      kind: "catalog";
      name: string;
      harness: string | null;
    }
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

type BrandIconKind = Exclude<NativeCodingAgentIconKind, "qwen">;
type CatalogBrandIconKind = Exclude<BrandIconKind, "opencode">;

// Qwen has no glyph yet; see docs/QWEN_FOLLOWUPS.md.
const BRAND_ICONS: Record<BrandIconKind, AgentIcon> = {
  antigravity: AntigravityIcon,
  claude: ClaudeIcon,
  codex: CodexIcon,
  cursor: CursorIcon,
  goose: GooseIcon,
  hermes: HermesIcon,
  kimi: KimiIcon,
  kiro: KiroIcon,
  opencode: OpenCodeIcon,
  // Exact match avoids false positives such as "openapi".
  pi: PiIcon,
};

type ExhaustiveOrder<Kind extends string, Order extends readonly Kind[]> =
  Exclude<Kind, Order[number]> extends never ? Order : never;

function defineBrandOrder<Kind extends string>() {
  return <const Order extends readonly Kind[]>(order: ExhaustiveOrder<Kind, Order>): Order => order;
}

// Keep Hermes last so it remains the lowest-priority root brand.
const ROOT_BRAND_ORDER = defineBrandOrder<BrandIconKind>()([
  "claude",
  "codex",
  "opencode",
  "cursor",
  "kiro",
  "goose",
  "kimi",
  "antigravity",
  "pi",
  "hermes",
]);

// OpenCode deliberately has no catalog harness-substring fallback.
const CATALOG_BRAND_ORDER = defineBrandOrder<CatalogBrandIconKind>()([
  "codex",
  "claude",
  "cursor",
  "hermes",
  "kiro",
  "goose",
  "kimi",
  "pi",
  "antigravity",
]);

function brandIconForKind(kind: NativeCodingAgentIconKind | undefined): AgentIcon | undefined {
  return kind === undefined || kind === "qwen" ? undefined : BRAND_ICONS[kind];
}

function harnessMatchesBrand(harness: string | null, kind: BrandIconKind): boolean {
  return kind === "pi" ? harness === "pi" : (harness?.includes(kind) ?? false);
}

function iconForAgentType(tool: string | null): AgentIcon {
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

function iconForRoot(source: Extract<AgentIconSource, { kind: "root" }>): AgentIcon {
  const nativeIconKind = nativeCodingAgentForWrapper(source.wrapper)?.iconKind;
  const brand = ROOT_BRAND_ORDER.find(
    (kind) => nativeIconKind === kind || harnessMatchesBrand(source.harness, kind),
  );
  if (brand !== undefined) return BRAND_ICONS[brand];
  if (source.agentName === "nessie") return NessieIcon;
  return BotIcon;
}

function iconForCatalog(source: Extract<AgentIconSource, { kind: "catalog" }>): AgentIcon {
  // Nessie uses claude-sdk, so its name must win over the harness fallback.
  if (source.name === "nessie") return NessieIcon;
  const nativeIconKind = nativeCodingAgentForAvailableAgent(source)?.iconKind;
  const nativeIcon = brandIconForKind(nativeIconKind);
  if (nativeIcon !== undefined) return nativeIcon;

  const harnessBrand = CATALOG_BRAND_ORDER.find((kind) =>
    harnessMatchesBrand(source.harness, kind),
  );
  if (harnessBrand !== undefined) return BRAND_ICONS[harnessBrand];
  return BotIcon;
}

/** Resolve the decorative glyph for an agent using its context-specific fallback rules. */
export function resolveAgentIcon(source: AgentIconSource): AgentIcon {
  if (source.kind === "root") return iconForRoot(source);
  if (source.kind === "catalog") return iconForCatalog(source);

  // A native session's sub-agents all share its brand, so repeating that logo
  // down the tree conveys nothing. Their `-subagent` wrappers intentionally do
  // not resolve here, letting role icons distinguish what each task is doing.
  const nativeIconKind = nativeCodingAgentForWrapper(source.wrapper)?.iconKind;
  const nativeIcon = brandIconForKind(nativeIconKind);
  if (nativeIcon !== undefined) return nativeIcon;
  // Pi scaffold children have no wrapper label, so their exact tool name is authoritative.
  if (source.tool === "pi") return PiIcon;
  return iconForAgentType(source.tool);
}
