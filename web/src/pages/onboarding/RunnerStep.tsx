// Onboarding: "Where do you work today?", shown after picking a preset server
// from the landing. Pick the runner (a remote environment when offered, else
// this laptop), then install/open.

import { useState } from "react";
import { Cloud, Laptop } from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { ConnectProgress } from "@/pages/onboarding/ServerSelectorV2";
import {
  ConnectStatus,
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
  connection = null,
  onCancelConnect,
  onBack,
  onInstall,
}: {
  /** Offer the remote environment, and pick it by default. */
  remoteAvailable: boolean;
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
  const [runner, setRunner] = useState<Runner>(remoteAvailable ? "remote" : "local");

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <OnboardingHeading>Where do you work today?</OnboardingHeading>

      {remoteAvailable && (
        <label htmlFor="runner-remote">
          <Card
            className={cn(
              "cursor-pointer flex-row pl-4 border-transparent items-center",
              runner === "remote" ? "shadow-sm" : "shadow-none",
            )}
          >
            <input
              id="runner-remote"
              type="radio"
              name="runner"
              checked={runner === "remote"}
              onChange={() => setRunner("remote")}
              className="size-[13px] shrink-0 appearance-none rounded-full border border-foreground text-foreground checked:bg-[radial-gradient(circle_at_center,currentColor_0_3px,transparent_3.5px)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <span className="flex items-center justify-center gap-1">
              <Cloud className="size-4" aria-hidden />
              Arca
            </span>
          </Card>
        </label>
      )}
      <div className="mt-2" />
      <label htmlFor="runner-local">
        <Card
          className={cn(
            "cursor-pointer flex-row pl-4 items-center",
            runner === "local" ? "shadow-sm" : "shadow-none",
          )}
        >
          <input
            id="runner-local"
            type="radio"
            name="runner"
            checked={runner === "local"}
            onChange={() => setRunner("local")}
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
          onClick={() => onInstall(runner)}
        />
      </OnboardingRail>
    </div>
  );
}
