// Onboarding: local-setup intro, shown after "Get started locally" on the
// landing. Explains what the local install gives you, then Install/Open starts
// the local server. See the design prototype (New + Native → "Get started
// locally").

import { Laptop } from "lucide-react";
import {
  BRAND_HARNESSES,
  HarnessBrandIcon,
  HarnessIconTile,
} from "@/components/onboarding/harnessBrand";
import {
  InstallActionButton,
  OnboardingBackButton,
  OnboardingHeading,
  OnboardingRail,
} from "@/pages/onboarding/primitives";

/** Overlapping harness-icon row shown in the panel band above the local intro. */
export function HarnessIconRow() {
  return (
    <div className="flex -space-x-2">
      <HarnessIconTile accent>
        <Laptop className="size-6" />
      </HarnessIconTile>
      {BRAND_HARNESSES.map((harness) => (
        <HarnessIconTile key={harness}>
          <HarnessBrandIcon harness={harness} size={24} />
        </HarnessIconTile>
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
