import type { ReactNode } from "react";
import { ArrowLeft } from "lucide-react";
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

export function installActionLabel(installed?: boolean): string {
  return installed ? "Open Omnigent" : "Install Omnigent";
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
  onClick,
}: {
  installed?: boolean;
  onClick: () => void;
}) {
  return (
    <Button size="lg" onClick={onClick}>
      {installActionLabel(installed)}
    </Button>
  );
}
