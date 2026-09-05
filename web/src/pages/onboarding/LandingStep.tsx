// Onboarding step 1: the hero landing. "Get started" opens deployment-mode
// select; "Join a server" opens server select. Rendered inside the card body
// below the animated panel (see AnimatedOmnigentPanel).

import { ArrowRight, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";

export function LandingStep({
  onGetStarted,
  onJoinServer,
}: {
  onGetStarted: () => void;
  onJoinServer: () => void;
}) {
  return (
    <div className="mt-auto flex flex-col gap-2 px-2 pb-1">
      <div className="mb-2 text-center">
        <h1 className="text-[32px] font-normal leading-9 tracking-[-0.03em] text-foreground">
          Meet Omnigent
        </h1>
        <p className="mt-1 text-base text-muted-foreground">One harness for every AI agent</p>
      </div>
      <Button onClick={onGetStarted} className="py-5">
        Get started
        <ArrowRight className="size-4" />
      </Button>
      <Button variant="outline" onClick={onJoinServer} className="py-5">
        Join a server
        <ChevronDown className="size-4" />
      </Button>
    </div>
  );
}
