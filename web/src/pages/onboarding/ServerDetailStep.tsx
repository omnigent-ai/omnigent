// Onboarding: single preset-server detail, shown after picking an MDM server
// from the landing split button. No list / no selection — just this server's
// details and the install/open action. See the design prototype (New + Native
// + Preset → "Join your team").

import { type ComponentType, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  Play,
  TabletSmartphone,
  Users,
  type LucideProps,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ServerDetails } from "@/pages/onboarding/ServerSelectStep";
import { installActionLabel, OnboardingHeading } from "@/pages/onboarding/primitives";
import { cn } from "@/lib/utils";

/** Strip scheme + trailing slash for display. */
function displayName(url: string): string {
  return url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
}

const BENEFITS: { label: string; icon: ComponentType<LucideProps> }[] = [
  { label: "Access agents from any device", icon: TabletSmartphone },
  { label: "Share sessions with your teammates", icon: Users },
  { label: "Keep sessions running in the cloud", icon: Play },
];

export function ServerDetailStep({
  url,
  installed,
  onBack,
  onConnect,
  onCopy,
}: {
  url: string;
  /** Returning user (CLI installed) → "Open Omnigent"; new → "Install Omnigent". */
  installed?: boolean;
  onBack: () => void;
  onConnect: (url: string) => void;
  onCopy: (text: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const name = displayName(url);

  return (
    <div className="flex h-full flex-col px-2 pb-1 pt-3">
      <OnboardingHeading>You&apos;re in!</OnboardingHeading>

      <div className="rounded-lg border border-border px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate text-base text-foreground">{name}</span>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted"
            aria-label={`${expanded ? "Collapse" : "Expand"} ${name}`}
            aria-expanded={expanded}
          >
            <ChevronDown className={cn("size-4 transition-transform", expanded && "rotate-180")} />
          </button>
        </div>

        {expanded && <ServerDetails url={url} onCopy={onCopy} />}
      </div>

      <div className="mt-4 flex flex-1 flex-col gap-2">
        {BENEFITS.map((b) => (
          <span key={b.label} className="flex gap-2 text-base text-muted-foreground">
            <b.icon className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span>{b.label}</span>
          </span>
        ))}
      </div>

      <div className="mt-3 flex justify-between gap-2">
        <Button variant="ghost" onClick={onBack} size="lg">
          <ArrowLeft className="size-4" />
          Back
        </Button>
        <Button onClick={() => onConnect(url)} size="lg">
          {installActionLabel(installed)}
        </Button>
      </div>
    </div>
  );
}
