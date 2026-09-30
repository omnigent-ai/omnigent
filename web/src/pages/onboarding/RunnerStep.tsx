// Onboarding: "Where do you work today?", shown after picking a preset server
// from the landing. Pick the runner (a remote environment when offered, else
// this laptop), then install/open.

import { useState } from "react";
import { Cloud, Laptop } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  InstallActionButton,
  OnboardingBackButton,
  OnboardingHeading,
  OnboardingRail,
} from "@/pages/onboarding/primitives";

export type Runner = "remote" | "local";

export function RunnerStep({
  remoteAvailable,
  installed,
  error,
  onBack,
  onInstall,
}: {
  /** Offer the remote environment, and pick it by default. */
  remoteAvailable: boolean;
  /** Returning user (CLI installed) → "Open Omnigent"; new → "Install Omnigent". */
  installed?: boolean;
  /** A connect error to show above the actions. */
  error?: string;
  onBack: () => void;
  onInstall: (runner: Runner) => void;
}) {
  const [runner, setRunner] = useState<Runner>(remoteAvailable ? "remote" : "local");

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <OnboardingHeading>Where do you work today?</OnboardingHeading>

      <Select value={runner} onValueChange={(v) => setRunner(v as Runner)}>
        <SelectTrigger aria-label="Runner" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {remoteAvailable && (
            <SelectItem value="remote">
              <Cloud className="size-4" aria-hidden />
              Arca
            </SelectItem>
          )}
          <SelectItem value="local">
            <Laptop className="size-4" aria-hidden />
            My laptop
          </SelectItem>
        </SelectContent>
      </Select>
      {runner === "local" && (
        // Onboarding connects this laptop without the host-enrollment prompt, so say what it grants.
        <p className="mt-2 text-center text-sm text-muted-foreground">
          The server will be able to run agents on this laptop.
        </p>
      )}

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

      <OnboardingRail>
        <OnboardingBackButton onClick={onBack} />
        <InstallActionButton installed={installed} onClick={() => onInstall(runner)} />
      </OnboardingRail>
    </div>
  );
}
