// Colored harness glyphs and the bordered tile they sit in, shared by the
// onboarding panel band and the post-setup import modal.

import type { ReactNode } from "react";
// The Color subpath keeps antd out of the bundle. Cursor has no Color
// variant, so it uses Mono.
import ClaudeCodeColor from "@lobehub/icons/es/ClaudeCode/components/Color";
import CodexColor from "@lobehub/icons/es/Codex/components/Color";
import CursorMono from "@lobehub/icons/es/Cursor/components/Mono";
import { NATIVE_CODING_AGENTS, type NativeCodingAgentIconKind } from "@/lib/nativeCodingAgents";
import { cn } from "@/lib/utils";

/** Harnesses with a brand glyph, keyed like `NATIVE_CODING_AGENTS`. */
export type BrandHarness = Extract<NativeCodingAgentIconKind, "claude" | "codex" | "cursor">;

export const BRAND_HARNESSES: readonly BrandHarness[] = ["claude", "codex", "cursor"];

export function HarnessBrandIcon({ harness, size }: { harness: BrandHarness; size: number }) {
  if (harness === "claude") return <ClaudeCodeColor size={size} />;
  if (harness === "codex") return <CodexColor size={size} />;
  return <CursorMono size={size} />;
}

export function harnessDisplayName(harness: BrandHarness): string {
  return NATIVE_CODING_AGENTS.find((agent) => agent.key === harness)?.displayName ?? harness;
}

/** 48px bordered tile for the panel band; `accent` tints it with the brand pink. */
export function HarnessIconTile({
  children,
  accent = false,
}: {
  children: ReactNode;
  accent?: boolean;
}) {
  return (
    <span
      className={cn(
        "flex size-12 items-center justify-center rounded-xl border bg-background",
        accent ? "border-brand-accent/25 text-brand-accent" : "border-border",
      )}
    >
      {children}
    </span>
  );
}
