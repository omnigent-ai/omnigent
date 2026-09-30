import type { ReactNode } from "react";
import { ArrowLeft, ArrowRight, Download, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function OnboardingHeading({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <h1
      className={cn(
        "mb-3 pt-1 text-center text-2xl font-normal leading-9 tracking-[-0.03em] text-foreground",
        className,
      )}
    >
      {children}
    </h1>
  );
}

/** Main action label: install the CLI first, start the stopped local server, or
 *  just open (a running local server or a remote one). */
export function installActionLabel(installed?: boolean, startsLocal?: boolean): string {
  if (!installed) return "Install Omnigent";
  return startsLocal ? "Start Omnigent" : "Open Omnigent";
}

/** Leading icon for the install/start/open action, paired with installActionLabel. */
export function InstallActionIcon({
  installed,
  startsLocal,
}: {
  installed?: boolean;
  startsLocal?: boolean;
}) {
  const Icon = !installed ? Download : startsLocal ? Play : ArrowRight;
  return <Icon className="size-4" aria-hidden />;
}

export function OnboardingRail({ children }: { children: ReactNode }) {
  return <div className="mt-3 flex justify-between gap-2">{children}</div>;
}

export function OnboardingBackButton({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" size="lg" onClick={onClick}>
      <ArrowLeft className="size-4" />
      Back
    </Button>
  );
}

export function InstallActionButton({
  installed,
  startsLocal,
  onClick,
}: {
  installed?: boolean;
  startsLocal?: boolean;
  onClick: () => void;
}) {
  return (
    <Button size="lg" onClick={onClick}>
      <InstallActionIcon installed={installed} startsLocal={startsLocal} />
      {installActionLabel(installed, startsLocal)}
    </Button>
  );
}
