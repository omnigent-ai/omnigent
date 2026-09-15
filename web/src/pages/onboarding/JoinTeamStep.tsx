// Invite-accept landing shown before the register form in the v2 flow. Accept
// just advances to the form — the account is created on form submit.

import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";

export function JoinTeamStep({ onAccept }: { onAccept: () => void }) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col items-center gap-1 text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">Join your team</h1>
        <p className="text-ui text-muted-foreground">
          Connect to their Omnigent server to start collaborating.
        </p>
      </div>
      <Button className="w-full gap-1 py-5" onClick={onAccept} componentId="register.accept_invite">
        Accept invitation
        <ArrowRight className="size-4" aria-hidden />
      </Button>
    </div>
  );
}

export default JoinTeamStep;
