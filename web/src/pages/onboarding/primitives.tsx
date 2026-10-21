import type { ReactNode } from "react";
import { ArrowLeft, ArrowRight, Download, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ConnectProgress } from "@/pages/onboarding/ServerSelectorV2";

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

export function OnboardingBackButton({
  onClick,
  disabled,
}: {
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Button variant="ghost" size="lg" onClick={onClick} disabled={disabled}>
      <ArrowLeft className="size-4" />
      Back
    </Button>
  );
}

/** What an in-flight connect is waiting on, with a Cancel when the shell supports it. */
export function ConnectStatus({
  connection,
  onCancel,
}: {
  connection: ConnectProgress | null;
  onCancel?: () => void;
}) {
  if (connection === null) return null;
  const { phase, error } = connection;
  return (
    <>
      <p role="status" className="mt-2 text-center text-sm text-muted-foreground">
        {phase === "cancelling"
          ? "Cancelling…"
          : phase === "authenticating"
            ? "Finish signing in in your browser, then come back here."
            : "Waiting for the server…"}{" "}
        {onCancel && phase !== "cancelling" && (
          <button type="button" onClick={onCancel} className="underline hover:text-foreground">
            Cancel
          </button>
        )}
      </p>
      {error && (
        <p role="alert" className="text-center text-sm text-destructive">
          {error}
        </p>
      )}
    </>
  );
}

export function InstallActionButton({
  installed,
  startsLocal,
  loading,
  onClick,
}: {
  installed?: boolean;
  startsLocal?: boolean;
  loading?: boolean;
  onClick: () => void;
}) {
  return (
    <Button size="lg" onClick={onClick} loading={loading}>
      <InstallActionIcon installed={installed} startsLocal={startsLocal} />
      {installActionLabel(installed, startsLocal)}
    </Button>
  );
}
