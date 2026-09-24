// Onboarding: local-setup intro, shown after "Get started locally" on the
// landing. Explains what the local install gives you, then Install/Open starts
// the local server. See the design prototype (New + Native → "Get started
// locally").

import type { ReactNode } from "react";
// Colored (brand) harness glyphs for the panel band — the Color subpath keeps
// antd out of the bundle. Cursor has no Color variant, so it uses Mono.
import ClaudeCodeColor from "@lobehub/icons/es/ClaudeCode/components/Color";
import CodexColor from "@lobehub/icons/es/Codex/components/Color";
import CursorMono from "@lobehub/icons/es/Cursor/components/Mono";
import { Laptop } from "lucide-react";
import {
  InstallActionButton,
  OnboardingBackButton,
  OnboardingHeading,
  OnboardingRail,
} from "@/pages/onboarding/primitives";
import { cn } from "@/lib/utils";

const LOCAL_ICONS: { key: string; node: ReactNode; accent?: boolean }[] = [
  { key: "local", node: <Laptop className="size-6" />, accent: true },
  { key: "claude", node: <ClaudeCodeColor size={24} /> },
  { key: "codex", node: <CodexColor size={24} /> },
  { key: "cursor", node: <CursorMono size={24} /> },
];

/** Overlapping harness-icon row shown in the panel band above the local intro. */
export function HarnessIconRow() {
  return (
    <div className="flex -space-x-2">
      {LOCAL_ICONS.map(({ key, node, accent }) => (
        <span
          key={key}
          className={cn(
            "flex size-12 items-center justify-center rounded-xl border bg-background",
            accent ? "border-brand-accent/25 text-brand-accent" : "border-border",
          )}
        >
          {node}
        </span>
      ))}
    </div>
  );
}

export function LocalIntroStep({
  installed,
  onBack,
  onInstall,
}: {
  /** Returning user (CLI installed) → "Open Omnigent"; new → "Install Omnigent". */
  installed?: boolean;
  onBack: () => void;
  onInstall: () => void;
}) {
  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-8">
      <OnboardingHeading>Set up Omnigent locally</OnboardingHeading>

      <p className="flex-1 text-base text-muted-foreground max-w-sm mx-auto text-center">
        Use Claude Code, Codex, Cursor, and other local harnesses from one UI. Keep history and
        context across harnesses. Import existing harness chats.
      </p>

      <OnboardingRail>
        <OnboardingBackButton onClick={onBack} />
        <InstallActionButton installed={installed} onClick={onInstall} />
      </OnboardingRail>
    </div>
  );
}
