// Onboarding: "Where do you work today?", shown after picking a preset server
// from the landing. Connect this laptop, then install/open.

import { Laptop } from "lucide-react";
import { Card } from "@/components/ui/card";
import type { ConnectProgress } from "@/pages/onboarding/ServerSelectorV2";
import {
  ConnectStatus,
  InstallActionButton,
  OnboardingBackButton,
  OnboardingHeading,
  OnboardingRail,
} from "@/pages/onboarding/primitives";

export type Runner = "local";

export function RunnerStep({
  installed,
  error,
  connection = null,
  onCancelConnect,
  onBack,
  onInstall,
}: {
  /** Returning user (CLI installed) → "Open Omnigent"; new → "Install Omnigent". */
  installed?: boolean;
  /** A connect error to show above the actions. */
  error?: string;
  /** Progress of an in-flight direct connect (null when idle). */
  connection?: ConnectProgress | null;
  onCancelConnect?: () => void;
  onBack: () => void;
  onInstall: (runner: Runner) => void;
}) {
  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <OnboardingHeading>Where do you work today?</OnboardingHeading>

      <div className="mt-2" />
      <label htmlFor="runner-local">
        <Card className="cursor-pointer flex-row pl-4 items-center shadow-sm">
          <input
            id="runner-local"
            type="radio"
            name="runner"
            checked
            readOnly
            className="size-[13px] shrink-0 appearance-none rounded-full border border-foreground text-foreground checked:bg-[radial-gradient(circle_at_center,currentColor_0_3px,transparent_3.5px)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          />
          <div className="flex flex-col">
            <span className="flex items-center justify-start gap-1">
              <Laptop className="size-4" aria-hidden />
              My laptop
            </span>

            <span className="text-center text-sm text-muted-foreground">
              The server will be able to run agents on this laptop.
            </span>
          </div>
        </Card>
      </label>

      <p className="mx-auto mt-4 max-w-sm flex-1 text-center text-base text-muted-foreground">
        Automatically carry over your existing setup. Share sessions with your teammates. Use from
        any device. Keep sessions running in the cloud.
      </p>

      {error && (
        <div role="alert" className="text-base text-destructive">
          <span className="font-medium">Couldn&apos;t connect to the server: </span>
          {error}
        </div>
      )}

      <ConnectStatus connection={connection} onCancel={onCancelConnect} />

      <OnboardingRail>
        <OnboardingBackButton onClick={onBack} disabled={connection !== null} />
        <InstallActionButton
          installed={installed}
          loading={connection !== null}
          onClick={() => onInstall("local")}
        />
      </OnboardingRail>
    </div>
  );
}
