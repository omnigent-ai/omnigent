// Plan-review body rendered inside `ApprovalCard` when the gated tool
// is Claude Code's built-in ExitPlanMode (claude-native sessions).
//
// Mirrors Claude's native plan-approval dialog:
//
//   1. "Yes, and use auto mode" — approve the plan AND switch the
//      session into `auto` mode (the server echoes a `setMode`
//      permission update on the verdict), so the plan executes without
//      per-tool prompts.
//   2. "Yes, manually approve edits" — approve the plan; the server
//      pins the session to the prompting `default` mode so every
//      edit prompts.
//   3. "Reject with feedback" — reveals a textarea; the typed feedback
//      rides on `content.feedback` and the server forwards it to
//      Claude as the deny `message`, so Claude stays in plan mode and
//      revises toward it.
//
// The plan markdown renders through the same secure Streamdown stack
// as assistant chat bubbles, so file paths and file links a plan names
// open in the FileViewer just as they do in a message.

import { CheckIcon, XIcon, ZapIcon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { MOBILE_CHOICE_CLASS, MOBILE_CHOICE_LIST_CLASS } from "./mobileChoiceClasses";

interface ExitPlanModeReviewProps {
  /** Plan markdown from the ExitPlanMode tool_input, e.g. `"# Plan\n…"`. */
  plan: string;
  /** Approve AND switch the session into Claude's `auto` mode. */
  onAcceptAuto: () => void;
  /** Approve; the session is pinned to `default` mode (prompts per edit). */
  onAccept: () => void;
  /** Reject; `feedback` is the user's typed revision guidance (`""` when none). */
  onReject: (feedback: string) => void;
}

export function ExitPlanModeReview({
  plan,
  onAcceptAuto,
  onAccept,
  onReject,
}: ExitPlanModeReviewProps) {
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");

  return (
    <div className="flex flex-col gap-2" data-testid="exit-plan-mode-review">
      {/* text-foreground: the plan body is the card's primary content, so it
          renders in normal text color like a regular assistant message —
          escaping AlertDescription's muted default (which otherwise washes
          the whole plan out). The short lead-in caption above stays muted
          for hierarchy, matching the Codex command card. */}
      <div className="text-ui text-foreground">
        <FilePathAwareMessageResponse>{plan}</FilePathAwareMessageResponse>
      </div>
      {rejecting ? (
        <div className="flex flex-col gap-2 pt-1" data-testid="exit-plan-mode-feedback">
          <Textarea
            autoFocus
            placeholder="What should change about the plan? (optional)"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            className="min-h-20 text-ui"
            componentId="plan.feedback"
          />
          <div className={cn("flex flex-wrap gap-2", MOBILE_CHOICE_LIST_CLASS)}>
            <Button
              size="sm"
              className={MOBILE_CHOICE_CLASS}
              onClick={() => onReject(feedback)}
              componentId="plan.reject"
            >
              <XIcon className="mr-1 size-3.5" />
              Reject plan
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className={cn(MOBILE_CHOICE_CLASS, "mobile-app:mt-2")}
              onClick={() => setRejecting(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <div className={cn("flex flex-wrap gap-2 pt-1", MOBILE_CHOICE_LIST_CLASS)}>
          <Button
            size="sm"
            className={MOBILE_CHOICE_CLASS}
            onClick={onAcceptAuto}
            componentId="plan.accept_auto"
          >
            <ZapIcon className="mr-1 size-3.5" />
            Yes, and use auto mode
          </Button>
          <Button
            size="sm"
            variant="outline"
            className={MOBILE_CHOICE_CLASS}
            onClick={onAccept}
            componentId="plan.accept_manual"
          >
            <CheckIcon className="mr-1 size-3.5" />
            Yes, manually approve edits
          </Button>
          <Button
            size="sm"
            variant="outline"
            className={cn(MOBILE_CHOICE_CLASS, "mobile-app:mt-2")}
            onClick={() => setRejecting(true)}
          >
            <XIcon className="mr-1 size-3.5" />
            Reject with feedback
          </Button>
        </div>
      )}
    </div>
  );
}
